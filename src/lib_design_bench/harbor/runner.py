"""Launch Harbor trial batches with retries, diagnostics, and a live summary."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections import Counter
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Coroutine
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import cast
from typing import override

import structlog
from harbor.models.job.config import RetryConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import ExceptionInfo
from harbor.models.trial.result import TrialResult
from harbor.trial.hooks import TrialEvent
from harbor.trial.hooks import TrialHookEvent
from harbor.trial.queue import TrialQueue as HarborTrialQueue
from harbor.trial.trial import Trial
from rich.console import Console
from rich.console import ConsoleOptions
from rich.console import Group
from rich.console import RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lib_design_bench.harbor.sandbox_costs import SandboxCostTracker
from lib_design_bench.logging import get_rich_console
from lib_design_bench.models.reports import AGENT_LIMIT_EXCEPTIONS
from lib_design_bench.models.reports import LIMIT_RECORD_NAME
from lib_design_bench.models.reports import LimitRecord
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.reports.trials import trial_error_reason
from lib_design_bench.reports.trials import trial_issues

logger = structlog.get_logger(__name__)


_BATCH_HEARTBEAT_INTERVAL_SECONDS = 30.0


@dataclass
class TrialLifecycle:
    """The latest observed lifecycle transition for one evaluation trial."""

    state: str
    transitioned_at: float


class BatchDiagnostics:
    """Track trial states so an interrupted batch identifies its stuck task."""

    def __init__(self, trial_names: Sequence[str]) -> None:
        now = time.monotonic()
        self._lifecycles = {
            trial_name: TrialLifecycle("queued", now) for trial_name in trial_names
        }

    def transition(self, trial_name: str, state: str) -> dict[str, float | str]:
        """Record a lifecycle transition and return its timing log fields."""
        now = time.monotonic()
        lifecycle = self._lifecycles[trial_name]
        previous_state = lifecycle.state
        previous_state_age_seconds = round(now - lifecycle.transitioned_at, 3)
        lifecycle.state = state
        lifecycle.transitioned_at = now
        return {
            "previous_state": previous_state,
            "previous_state_age_seconds": previous_state_age_seconds,
            "state": state,
        }

    def summary(self) -> dict[str, int]:
        """Return trial counts by lifecycle state without per-trial payloads."""
        return dict(Counter(lifecycle.state for lifecycle in self._lifecycles.values()))


async def run_tracked_batch[T](
    tasks: Mapping[str, asyncio.Task[T]],
    diagnostics: BatchDiagnostics,
    *,
    logger: Any,
    log_context: Mapping[str, object],
) -> list[T]:
    """Await a batch while preserving cancellation and orphan-task diagnostics."""
    heartbeat_task = asyncio.create_task(
        _log_batch_heartbeats(tasks, diagnostics, logger, log_context),
        name="harbor-trial-batch-heartbeat",
    )
    batch_task = asyncio.create_task(
        await_task_results(list(tasks.values())),
        name="harbor-trial-batch-results",
    )
    try:
        return await asyncio.shield(batch_task)
    except asyncio.CancelledError:
        logger.warning(
            "Evaluation batch cancellation received.",
            pending_trial_count=sum(not task.done() for task in tasks.values()),
            **log_context,
        )
        pending_tasks = [task for task in tasks.values() if not task.done()]
        for task in pending_tasks:
            task.cancel()
        logger.debug(
            "Requested cancellation of pending evaluation trials.",
            pending_trial_count=len(pending_tasks),
            trial_states=diagnostics.summary(),
            **log_context,
        )
        try:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            with contextlib.suppress(asyncio.CancelledError):
                await batch_task
        except asyncio.CancelledError:
            logger.warning(
                "Evaluation batch cancellation interrupted while cleanup was pending.",
                pending_trial_count=sum(not task.done() for task in tasks.values()),
                **log_context,
            )
            raise
        logger.debug(
            "Evaluation batch cancellation cleanup completed.",
            trial_states=diagnostics.summary(),
            remaining_task_count=len(
                asyncio.all_tasks()
                - {*tasks.values(), heartbeat_task, batch_task, asyncio.current_task()}
            ),
            **log_context,
        )
        raise
    except Exception:
        logger.debug(
            "Evaluation batch failed before returning results.",
            trial_states=diagnostics.summary(),
            exc_info=True,
            **log_context,
        )
        raise
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


async def await_task_results[T](
    tasks: Sequence[asyncio.Task[T]],
) -> list[T]:
    """Await tasks in submission order for a shielded batch runner."""
    return await asyncio.gather(*tasks)


async def _log_batch_heartbeats(
    tasks: Mapping[str, asyncio.Task[Any]],
    diagnostics: BatchDiagnostics,
    logger: Any,
    log_context: Mapping[str, object],
) -> None:
    while True:
        await asyncio.sleep(_BATCH_HEARTBEAT_INTERVAL_SECONDS)
        logger.debug(
            "Evaluation batch heartbeat.",
            pending_trial_count=sum(not task.done() for task in tasks.values()),
            trial_states=diagnostics.summary(),
            **log_context,
        )


class TrialQueue(HarborTrialQueue):
    """Preserve verification and evidence around Harbor's native trial retries."""

    def __init__(
        self,
        n_concurrent: int,
        retry_config: RetryConfig,
        run_dir: Path,
    ) -> None:
        super().__init__(n_concurrent=n_concurrent, retry_config=retry_config)
        self._attempts: dict[str, int] = {}
        self.sandbox_costs = SandboxCostTracker(run_dir)

    @override
    def _setup_hooks(self, trial: Trial) -> None:
        super()._setup_hooks(trial)
        run_agent = trial._run_agent_phase
        run_trial = trial.run
        name = trial.config.trial_name
        attempt = self._attempts.get(name, 0)
        self._attempts[name] = attempt + 1
        retries_left = attempt < self._retry_config.max_retries
        archive = trial.paths.trial_dir.parent / f".retries-{name}"
        agent_errors: list[ExceptionInfo] = []
        agent_limits: list[LimitRecord] = []
        sandbox_execution: int | None = None

        async def start_sandbox_cost(event: TrialHookEvent) -> None:
            nonlocal sandbox_execution
            sandbox_execution = self.sandbox_costs.start(trial, event)

        async def finish_sandbox_cost(event: TrialHookEvent) -> None:
            self.sandbox_costs.finish(trial, event, sandbox_execution)

        async def select_retry(_event: TrialHookEvent) -> None:
            result = trial.result
            errors = [
                error
                for error in (
                    result.exception_info,
                    *agent_errors,
                )
                if error is not None
            ]
            terminal = next(
                (
                    error
                    for error in errors
                    if not self._should_retry_exception(error.exception_type)
                ),
                None,
            )
            if terminal is not None:
                result.exception_info = terminal
            elif errors:
                result.exception_info = errors[0]
            else:
                return
            # Select only after verification finishes, and persist
            # before Harbor scrubs output. Original errors retain their sidecars.
            trial.paths.result_path.write_text(result.model_dump_json(indent=4))

        async def retain_attempt() -> TrialResult:
            try:
                result = await run_trial()
                if (
                    result.exception_info is not None
                    and result.exception_info.exception_type == "CancelledError"
                ):
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                shutil.rmtree(trial.paths.trial_dir)
                if archive.exists():
                    shutil.rmtree(archive)
                logger.info("Discarded cancelled trial.", trial_name=name)
                raise
            if (
                retries_left
                and result.exception_info is not None
                and self._should_retry_exception(result.exception_info.exception_type)
            ):
                # Trial.run has flushed logs and scrubbed secrets. Harbor deletes
                # the original directory before its next attempt.
                await asyncio.to_thread(
                    shutil.copytree, trial.paths.trial_dir, archive / str(attempt + 1)
                )
                logger.info(
                    "Retrying Harbor trial after execution failure.",
                    trial_name=name,
                    exception_type=result.exception_info.exception_type,
                    exception_message=result.exception_info.exception_message,
                    next_execution=attempt + 2,
                    max_executions=self._retry_config.max_retries + 1,
                )
            elif archive.exists():
                shutil.move(archive, trial.paths.trial_dir / "retries")
            return result

        async def finish_agent(**kwargs: Any) -> None:
            try:
                await run_agent(**kwargs)
            except Exception as error:
                issue = ExceptionInfo.from_exception(error)
                evidence = trial.paths.trial_dir
                (evidence / "agent-error.json").write_text(
                    issue.model_dump_json() + "\n", encoding="utf-8"
                )
                agent_errors.append(issue)
                limit = AGENT_LIMIT_EXCEPTIONS.get(issue.exception_type)
                if limit is not None:
                    agent_limits.append(
                        LimitRecord(
                            kind=limit,
                            detail=issue.exception_message,
                        )
                    )
                logger.debug(
                    "Agent execution failed; continuing to verification.",
                    retryable=self._should_retry_exception(issue.exception_type),
                    trial_name=trial.config.trial_name,
                    exception_type=issue.exception_type,
                    exception_message=issue.exception_message,
                    limit=limit,
                )

        async def record_limit(_event: TrialHookEvent) -> None:
            # Budget exhaustion is not readable from the saved result, so write
            # it beside agent-error.json, before Harbor scrubs and moves output.
            record = agent_limits[-1] if agent_limits else LimitRecord(kind="none")
            record.write(trial.paths.trial_dir / LIMIT_RECORD_NAME)
            if record.kind != "none":
                logger.debug(
                    "Recorded the limit that ended a trial's agent.",
                    trial_name=name,
                    kind=record.kind,
                    detail=record.detail,
                )

        # Harbor has no execution-error hook; adapt only this trial instance.
        cast(Any, trial)._run_agent_phase = finish_agent
        cast(Any, trial).run = retain_attempt
        trial.add_hook(TrialEvent.ENVIRONMENT_START, start_sandbox_cost)
        trial.add_hook(TrialEvent.END, finish_sandbox_cost)
        trial.add_hook(TrialEvent.END, select_retry)
        trial.add_hook(TrialEvent.END, record_limit)


@dataclass(frozen=True)
class RetainedTrialCompletion:
    """One previously published result to include in a resumed live summary."""

    trial_name: str
    result: TrialResult
    trial: TrialReport


async def launch_trials(
    trial_configs: Sequence[TrialConfig],
    *,
    run_dir: Path,
    setup: Sequence[str] = (),
    n_concurrent: int,
    completion_processor: CompletionProcessor | None = None,
    retained_completions: Sequence[RetainedTrialCompletion] = (),
) -> tuple[TrialResult, ...]:
    """Launch trials with Harbor retries and publish only their final results.

    `setup` names the model setups the batch runs, one panel line each.
    """
    configs = list(trial_configs)
    logger.info(
        "Launching Harbor trials with the Harbor API.",
        trial_count=len(configs),
        n_concurrent=n_concurrent,
    )
    return await _run_harbor_trial_queue(
        configs,
        run_dir=run_dir,
        setup=setup,
        n_concurrent=n_concurrent,
        completion_processor=completion_processor,
        retained_completions=retained_completions,
    )


CompletionProcessor = Callable[
    [TrialResult], Awaitable[tuple[TrialResult, TrialReport]]
]


RETRY_CONFIG = RetryConfig(
    max_retries=3,
    # Waits outlast a provider's rate-limit window: 60s, 120s, 240s.
    min_wait_sec=60,
    wait_multiplier=2,
    max_wait_sec=300,
    include_exceptions={
        "EnvironmentStartTimeoutError",
        "DaytonaError",
        "DaytonaRateLimitError",
        "DaytonaTimeoutError",
        "DaytonaConnectionError",
        "ApiRateLimitError",
        # Harbor's installed agents raise this for a provider 500.
        "ApiInternalServerError",
        "ApiOverloadedError",
        "ApiConnectionClosedError",
        "ApiResponseStalledError",
        "ApiConnectionError",
        "NetworkConnectionError",
        # LDB's workspace adapter aborts an agent whose sandbox lost
        # its provider network rather than letting it idle to timeout.
        "AgentNetworkStalledError",
        # Harbor's in-process agents can propagate SDK errors directly.
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ServiceUnavailableError",
    },
)
"""Which trial failures Harbor retries, and how long it backs off between them."""


async def _run_harbor_trial_queue(
    configs: list[TrialConfig],
    *,
    run_dir: Path,
    setup: Sequence[str],
    n_concurrent: int,
    completion_processor: CompletionProcessor | None,
    retained_completions: Sequence[RetainedTrialCompletion],
) -> tuple[TrialResult, ...]:
    """Run a Harbor batch and preserve diagnostics while cancellation cleans up."""
    if not configs:
        return ()

    queue = TrialQueue(
        n_concurrent=n_concurrent,
        retry_config=RETRY_CONFIG,
        run_dir=run_dir,
    )
    diagnostics = BatchDiagnostics([config.trial_name for config in configs])
    summary = _LiveTrialSummary(
        total=len(configs) + len(retained_completions),
        run_dir=run_dir,
        setup=tuple(setup),
        sandbox_tracker=queue.sandbox_costs,
        clock=lambda: datetime.now(UTC),
        started_at=datetime.now(UTC),
        retained=frozenset(
            completion.trial_name for completion in retained_completions
        ),
        labels={
            config.trial_name: config.task.source or config.trial_name
            for config in configs
        },
    )
    for completion in retained_completions:
        summary.record_finalized(
            completion.trial_name,
            completion.result,
            completion.trial,
        )
    with Live(
        summary,
        console=get_rich_console(),
        auto_refresh=False,
        vertical_overflow="crop",
    ) as live:
        progress_state = _add_progress_hooks(queue, diagnostics, summary, live)
        tasks = _submit_harbor_trials(
            queue, configs, progress_state, completion_processor=completion_processor
        )
        refresh_task = asyncio.create_task(
            _periodically_refresh_live_summary(live, summary),
            name="harbor-live-summary",
        )
        try:
            results = await run_tracked_batch(
                tasks,
                diagnostics,
                logger=logger,
                log_context={
                    "trial_count": len(configs),
                    "n_concurrent": n_concurrent,
                },
            )
        finally:
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task

    completed = tuple(results)
    logger.debug(
        "Completed Harbor trials.",
        trial_count=len(completed),
        trials_with_issues=sum(bool(trial_issues(result)) for result in completed),
    )
    return completed


def _submit_harbor_trials(
    queue: TrialQueue,
    configs: Sequence[TrialConfig],
    progress_state: _TrialProgressState,
    *,
    completion_processor: CompletionProcessor | None,
) -> dict[str, asyncio.Task[TrialResult]]:
    """Schedule logical Harbor trials and preserve their names for diagnostics."""
    tasks = {
        config.trial_name: asyncio.create_task(
            _await_trial_completion(
                config.trial_name, coroutine, progress_state, completion_processor
            ),
            name=f"harbor-trial:{config.trial_name}",
        )
        for config, coroutine in zip(
            configs, queue.submit_batch(list(configs)), strict=True
        )
    }
    for trial_name, task in tasks.items():
        logger.debug(
            "Submitted Harbor trial.", trial_name=trial_name, task_name=task.get_name()
        )
    return tasks


@dataclass
class _LiveTrialSummary:
    """Render finalized metrics without redrawing per-trial timers."""

    total: int
    run_dir: Path
    labels: Mapping[str, str]
    clock: Callable[[], datetime]
    started_at: datetime
    retained: frozenset[str]
    setup: tuple[str, ...] = ()
    sandbox_tracker: SandboxCostTracker | None = None
    started: set[str] = field(default_factory=set)
    active_stages: dict[str, str] = field(default_factory=dict)
    finalized: set[str] = field(default_factory=set)
    cancelled: set[str] = field(default_factory=set)
    incomplete: int = 0
    simplicities: list[float] = field(default_factory=list)
    rewards: list[float] = field(default_factory=list)
    pass_rates: list[float] = field(default_factory=list)
    input_tokens: list[float] = field(default_factory=list)
    output_tokens: list[float] = field(default_factory=list)
    api_costs: list[float] = field(default_factory=list)
    sandbox_costs: list[float] = field(default_factory=list)
    sandbox_accounted: set[str] = field(default_factory=set)
    eta: str = "—"

    def record_start(self, trial_name: str) -> bool:
        """Count a logical trial as active without treating retries as new work."""
        if trial_name in self.finalized:
            return False
        self.started.add(trial_name)
        return self._record_stage(trial_name, "Starting")

    def record_stage(self, trial_name: str, stage: str) -> bool:
        """Record one logical trial's current phase for the active table."""
        if trial_name in self.finalized:
            return False
        self.started.add(trial_name)
        return self._record_stage(trial_name, stage)

    def record_finalized(
        self,
        trial_name: str,
        result: TrialResult,
        trial: TrialReport | None,
    ) -> None:
        """Add one persisted logical-trial outcome to the live aggregate.

        `trial` is absent when the batch publishes no reports.
        """
        if trial_name in self.finalized:
            return
        self.started.add(trial_name)
        self.finalized.add(trial_name)
        self.active_stages.pop(trial_name, None)
        incomplete_reason = None if trial is None else trial.incomplete_reason
        if result.exception_info is not None or incomplete_reason is not None:
            self.incomplete += 1
        if trial is not None:
            if incomplete_reason is not None:
                # An incomplete cell scores zero reward but has
                # no simplicity or pass rate.
                self.rewards.append(0.0)
            else:
                for value, values in (
                    (trial.simplicity, self.simplicities),
                    (trial.reward, self.rewards),
                    (trial.pass_rate, self.pass_rates),
                ):
                    if value is not None:
                        values.append(value)
            for value, values in (
                (trial.usage.input_tokens, self.input_tokens),
                (trial.usage.output_tokens, self.output_tokens),
                (trial.usage.cost_usd, self.api_costs),
                (trial.sandbox_usage.cost_usd, self.sandbox_costs),
            ):
                if value is not None:
                    values.append(value)
            if trial.sandbox_usage.cost_usd is not None:
                self.sandbox_accounted.add(trial_name)
        self._record_eta(trial_name)

    def record_unreturned_failure(self, trial_name: str) -> None:
        """Settle a trial that cannot yield persisted aggregate measurements."""
        if trial_name in self.finalized:
            return
        self.started.add(trial_name)
        self.finalized.add(trial_name)
        self.active_stages.pop(trial_name, None)
        self.incomplete += 1
        self._record_eta(trial_name)

    def _record_eta(self, trial_name: str) -> None:
        """Extrapolate this session's finalization rate to the unfinished cells.

        Retained completions from an earlier session finish no work here, so
        they neither set nor speed up the estimate, which holds until the next
        finalization.
        """
        if trial_name in self.retained:
            return
        finished = len(self.finalized - self.retained)
        remaining = self.total - len(self.finalized | self.cancelled)
        elapsed = (self.clock() - self.started_at).total_seconds()
        self.eta = _duration(elapsed / finished * remaining)

    def _record_stage(self, trial_name: str, stage: str) -> bool:
        if self.active_stages.get(trial_name) == stage:
            return False
        self.active_stages[trial_name] = stage
        return True

    def render(self, width: int) -> Panel:
        """Build the compact summary shown during a Harbor batch.

        Every line is one terminal row at any width: a line that wrapped at a
        narrower terminal would change the box's height between redraws and
        leave stale rows behind after a resize. The run directory keeps its
        tail when it does not fit, since the leaf names the run.
        """
        label = "Saving to: "
        path = self.run_dir.as_posix()
        # The panel's border and padding take two columns on each side.
        room = width - 4 - len(label)
        if len(path) > room:
            path = "…" + path[len(path) - room + 1 :] if room > 1 else "…"
        saving = _metric_line((label.removesuffix(": "), path))
        counts = _live_line()
        counts.append(
            f"Finalized {len(self.finalized)}/{self.total}", style="bold cyan"
        )
        counts.append(
            f"  ·  Active {len(self.started - self.finalized - self.cancelled)}",
            style="cyan",
        )
        counts.append(
            f"  ·  Queued {self.total - len(self.started | self.cancelled)}",
            style="cyan",
        )
        if self.cancelled:
            counts.append(f"  ·  Cancelled {len(self.cancelled)}", style="yellow")
        counts.append(f"  ·  Incomplete {self.incomplete}", style="yellow")
        counts.append(f"  ·  ETA {self.eta}", style="cyan")
        means = _live_line()
        means.append("Mean reward ", style="dim")
        means.append(_mean_or_dash(self.rewards))
        means.append(f"  ·  simplicity {_mean_or_dash(self.simplicities)}")
        means.append(f"  ·  pass_rate {_mean_or_dash(self.pass_rates)}")
        output_tokens = _known_sum(self.output_tokens)
        tokens = _metric_line(
            (
                "Tokens",
                _tokens_or_dash(
                    _sum_known_costs(_known_sum(self.input_tokens), output_tokens)
                ),
            ),
            ("output", _tokens_or_dash(output_tokens)),
        )
        api_cost = _known_sum(self.api_costs)
        finalized_sandbox = _known_sum(self.sandbox_costs)
        has_tracked_sandbox, tracked_sandbox = (
            (False, None)
            if self.sandbox_tracker is None
            else self.sandbox_tracker.live_cost_usd(
                excluding=self.sandbox_accounted, now=self.clock()
            )
        )
        if not has_tracked_sandbox:
            tracked_sandbox = None
        sandbox_cost = _sum_known_costs(finalized_sandbox, tracked_sandbox)
        total_cost = _sum_known_costs(api_cost, sandbox_cost)
        costs = _metric_line(
            ("Cost", _cost_or_dash(total_cost)),
            ("API", _cost_or_dash(api_cost)),
            ("sandbox", _cost_or_dash(sandbox_cost)),
        )
        active_table = self._active_table()
        return Panel(
            Group(
                *(Text(line, no_wrap=True, overflow="ellipsis") for line in self.setup),
                saving,
                counts,
                means,
                tokens,
                costs,
                *(() if active_table is None else (active_table,)),
            ),
            title="[bold cyan]Harbor trial progress[/]",
            border_style="dim",
        )

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Recompute active sandbox accrual at the width of every Rich refresh."""
        yield self.render(options.max_width)

    def _active_table(self) -> Table | None:
        if not self.active_stages:
            return None
        table = Table(box=None, pad_edge=False, padding=(0, 1))
        table.add_column("Task/problem", ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column("Status", no_wrap=True)
        active = sorted(
            self.active_stages.items(),
            key=lambda item: (self.labels.get(item[0], item[0]), item[0]),
        )
        for trial_name, stage in active[:6]:
            table.add_row(self.labels.get(trial_name, trial_name), stage)
        remaining = len(active) - 6
        if remaining > 0:
            table.add_row(f"… {remaining} more active", "")
        return table


def _live_line() -> Text:
    """Start one summary line that truncates instead of wrapping."""
    return Text(no_wrap=True, overflow="ellipsis")


def _mean_or_dash(values: list[float]) -> str:
    """Format one persisted metric's running mean without implying absent data is zero."""
    return f"{sum(values) / len(values):.3f}" if values else "—"


def _metric_line(*fields: tuple[str, str]) -> Text:
    """Join labeled values into one row, separated like the count line."""
    line = _live_line()
    for index, (label, value) in enumerate(fields):
        if index:
            line.append("  ·  ", style="dim")
        line.append(f"{label}: ", style="dim")
        line.append(value, style="bold cyan")
    return line


def _known_sum(values: list[float]) -> float | None:
    return sum(values) if values else None


def _sum_known_costs(*costs: float | None) -> float | None:
    known = tuple(cost for cost in costs if cost is not None)
    return sum(known) if known else None


def _cost_or_dash(cost: float | None) -> str:
    return "—" if cost is None else f"${cost:,.3f}"


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m" if minutes else f"{secs}s"


def _tokens_or_dash(total: float | None) -> str:
    if total is None:
        return "—"
    for divisor, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if total >= divisor:
            return f"{total / divisor:.2f}{unit}"
    return f"{total:,.0f}"


@dataclass
class _TrialProgressState:
    """Coordinate logical trial diagnostics with the compact live summary."""

    diagnostics: BatchDiagnostics
    summary: _LiveTrialSummary
    live: Live


def _add_progress_hooks(
    queue: HarborTrialQueue,
    diagnostics: BatchDiagnostics,
    summary: _LiveTrialSummary,
    live: Live,
) -> _TrialProgressState:
    progress_state = _TrialProgressState(
        diagnostics=diagnostics,
        summary=summary,
        live=live,
    )

    async def on_start(event: TrialHookEvent) -> None:
        trial_name = _event_trial_name(event)
        logger.debug(
            "Started Harbor trial.",
            trial_name=trial_name,
            **diagnostics.transition(trial_name, "starting trial"),
        )
        if summary.record_start(trial_name):
            _refresh_live_summary(live, summary)

    def progress_handler(
        *,
        log_message: str,
        state: str,
        display_stage: str,
        warning: bool = False,
    ):
        async def on_progress(event: TrialHookEvent) -> None:
            trial_name = _log_trial_state(
                event,
                diagnostics,
                log_message=log_message,
                state=state,
                warning=warning,
            )
            if summary.record_stage(trial_name, display_stage):
                _refresh_live_summary(live, summary)

        return on_progress

    queue.add_hook(TrialEvent.START, on_start)
    queue.add_hook(
        TrialEvent.ENVIRONMENT_START,
        progress_handler(
            log_message="Started Harbor trial environment.",
            state="starting environment",
            display_stage="Environment",
        ),
    )
    queue.add_hook(
        TrialEvent.AGENT_START,
        progress_handler(
            log_message="Started Harbor trial agent.",
            state="running agent",
            display_stage="Agent",
        ),
    )
    queue.add_hook(
        TrialEvent.VERIFICATION_START,
        progress_handler(
            log_message="Started Harbor trial verification.",
            state="running verifier",
            display_stage="Verifier",
        ),
    )
    queue.add_hook(
        TrialEvent.CANCEL,
        progress_handler(
            log_message="Canceling Harbor trial.",
            state="canceling trial; this may take up to a minute",
            display_stage="Cancelling",
            warning=True,
        ),
    )
    return progress_state


async def _await_trial_completion(
    trial_name: str,
    coroutine: Coroutine[Any, Any, TrialResult],
    progress_state: _TrialProgressState,
    completion_processor: CompletionProcessor | None = None,
) -> TrialResult:
    """Publish the returned Harbor result and refresh its logical-trial summary."""
    try:
        result = await coroutine
        if (
            result.exception_info is not None
            and result.exception_info.exception_type == "CancelledError"
        ):
            raise asyncio.CancelledError
    except asyncio.CancelledError:
        transition = progress_state.diagnostics.transition(trial_name, "cancelled")
        progress_state.summary.active_stages.pop(trial_name, None)
        progress_state.summary.cancelled.add(trial_name)
        _refresh_live_summary(progress_state.live, progress_state.summary)
        logger.debug(
            "Harbor trial lifecycle cancelled.", trial_name=trial_name, **transition
        )
        raise
    except Exception as error:
        transition = progress_state.diagnostics.transition(trial_name, "failed")
        progress_state.summary.record_unreturned_failure(trial_name)
        _refresh_live_summary(progress_state.live, progress_state.summary)
        _log_unreturned_trial_error(trial_name, error)
        logger.debug(
            "Harbor trial lifecycle failed before returning a final result.",
            trial_name=trial_name,
            exc_info=True,
            **transition,
        )
        raise
    trial: TrialReport | None = None
    if completion_processor is not None:
        if progress_state.summary.record_stage(trial_name, "Publishing"):
            _refresh_live_summary(progress_state.live, progress_state.summary)
        try:
            result, trial = await completion_processor(result)
        except Exception as error:
            transition = progress_state.diagnostics.transition(trial_name, "failed")
            progress_state.summary.record_unreturned_failure(trial_name)
            _refresh_live_summary(progress_state.live, progress_state.summary)
            _log_unreturned_trial_error(trial_name, error)
            logger.debug(
                "Harbor trial publication failed before logical completion.",
                trial_name=trial_name,
                exc_info=True,
                **transition,
            )
            raise
    _log_trial_completion(trial_name, result, progress_state, trial)
    return result


def _log_trial_completion(
    trial_name: str,
    result: TrialResult,
    progress_state: _TrialProgressState,
    trial: TrialReport | None,
) -> None:
    """Record diagnostics and announce each returned, published trial."""
    failed = result.exception_info is not None
    if result.exception_info is not None:
        logger.debug(
            "Harbor trial failed on final result receipt.",
            trial_name=trial_name,
            exception_type=result.exception_info.exception_type,
        )
    incomplete_reason = None if trial is None else trial.incomplete_reason
    failed = failed or incomplete_reason is not None
    transition = progress_state.diagnostics.transition(
        trial_name, "failed" if failed else "finished"
    )
    progress_state.summary.record_finalized(trial_name, result, trial)
    _refresh_live_summary(progress_state.live, progress_state.summary)
    logger.debug(
        "Harbor trial completion diagnostics.",
        trial_name=trial_name,
        exception_type=(
            result.exception_info.exception_type
            if result.exception_info is not None
            else None
        ),
        **transition,
    )
    _log_trial_exception(trial_name, result)
    completion_log_fields = (
        {}
        if trial is None
        else {
            "reward": trial.reward,
            "pass_rate": trial.pass_rate,
            "simplicity": trial.simplicity,
            "api_cost": trial.usage.cost_usd,
            "sandbox_cost": trial.sandbox_usage.cost_usd,
            "elapsed": trial.usage.time_spent,
        }
    )
    logger.info(f'"{trial_name}" finished', **completion_log_fields)
    if failed:
        logger.debug(
            f'"{trial_name}" had error',
            incomplete_reason=(
                incomplete_reason
                or trial_error_reason(result)
                or "incomplete trial result"
            ),
            **completion_log_fields,
        )


async def _periodically_refresh_live_summary(
    live: Live, summary: _LiveTrialSummary
) -> None:
    while True:
        await asyncio.sleep(0.5)
        _refresh_live_summary(live, summary)


def _refresh_live_summary(live: Live, summary: _LiveTrialSummary) -> None:
    """Redraw as soon as a logical trial starts or finalizes."""
    live.update(summary, refresh=True)


def _log_unreturned_trial_error(trial_name: str, error: Exception) -> None:
    logger.debug(
        f'"{trial_name}" had error',
        score=None,
        cost=None,
        elapsed=None,
        simplicity=None,
        reward=None,
        incomplete_reason=f"{type(error).__name__}: {error}",
    )


def _log_trial_exception(trial_name: str, result: TrialResult | None) -> None:
    """Write the full exception payload only when a Harbor trial failed."""
    if result is None or result.exception_info is None:
        return
    logger.warning(
        "Harbor trial raised an exception.",
        trial_name=trial_name,
        exception_type=result.exception_info.exception_type,
        exception_message=result.exception_info.exception_message.splitlines()[0]
        if result.exception_info.exception_message
        else None,
    )
    logger.debug(
        "Harbor trial exception diagnostics.",
        trial_name=trial_name,
        exception_type=result.exception_info.exception_type,
        exception_message=result.exception_info.exception_message,
        exception_traceback=result.exception_info.exception_traceback,
    )


def _event_trial_name(event: TrialHookEvent) -> str:
    result = event.result
    if result is not None:
        return result.trial_name
    trial_name = getattr(event, "trial_name", None)
    if isinstance(trial_name, str) and trial_name:
        return trial_name
    return event.config.trial_name or str(event.trial_id)


def _log_trial_state(
    event: TrialHookEvent,
    diagnostics: BatchDiagnostics,
    *,
    log_message: str,
    state: str,
    warning: bool = False,
) -> str:
    trial_name = _event_trial_name(event)
    fields = diagnostics.transition(trial_name, state)
    if warning:
        logger.warning(log_message, trial_name=trial_name, **fields)
    else:
        logger.debug(log_message, trial_name=trial_name, **fields)
    return trial_name
