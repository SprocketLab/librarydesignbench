"""Behavioral tests for public Harbor logical-trial launching."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
import structlog
from daytona.common.errors import DaytonaError
from harbor.agents.installed.base import ApiUsageLimitError
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import TaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import AgentInfo
from harbor.models.trial.result import ExceptionInfo
from harbor.models.trial.result import TrialResult
from harbor.models.verifier.result import VerifierResult
from harbor.trial.hooks import HookCallback
from harbor.trial.hooks import TrialEvent
from harbor.trial.hooks import TrialHookEvent
from rich.console import Console
from structlog.testing import capture_logs

from lib_design_bench.harbor import runner
from lib_design_bench.models.reports import LIMIT_RECORD_NAME
from lib_design_bench.models.reports import LimitRecord
from lib_design_bench.models.reports import SandboxUsageReport
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.models.reports import UsageReport
from lib_design_bench.runs.store import load_sandbox_usage

_RUN_DIR = Path("/runs/agent_attempts/model/final__agent-high__2026-09-22T00-00-00Z")


def _report(
    trial_name: str,
    *,
    reward: float | None = None,
    pass_rate: float | None = None,
    simplicity: float | None = None,
    input_tokens: float | None = None,
    output_tokens: float | None = None,
    api_cost: float | None = None,
    sandbox_cost: float | None = None,
    incomplete_reason: str | None = None,
) -> TrialReport:
    """Return the persisted report a publisher hands back for one trial."""
    return TrialReport(
        schema_version=1,
        trial_name=trial_name,
        implementor="impl",
        task="task",
        problem="problem",
        library="no-library",
        agent="agent",
        model="model",
        reasoning="none",
        environment_type="docker",
        trial_path=trial_name,
        trial_uri=f"file:///{trial_name}",
        reward=reward,
        pass_rate=pass_rate,
        simplicity=simplicity,
        usage=UsageReport(
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=api_cost
        ),
        sandbox_usage=SandboxUsageReport(cost_usd=sandbox_cost),
        incomplete_reason=incomplete_reason,
    )


class _FakeTrialQueue:
    """Return finished logical-trial results at Harbor's external queue seam."""

    def __init__(self, results: tuple[TrialResult, ...]) -> None:
        self.results = results
        self.sandbox_costs = runner.SandboxCostTracker(_RUN_DIR)

    def add_hook(self, event: TrialEvent, callback: HookCallback) -> _FakeTrialQueue:
        return self

    def submit_batch(self, configs: list[TrialConfig]) -> list[Awaitable[TrialResult]]:
        assert [config.trial_name for config in configs] == [
            result.trial_name for result in self.results
        ]

        async def finished(result: TrialResult) -> TrialResult:
            return result

        return [finished(result) for result in self.results]


def _result(
    trial_name: str,
    reward: float | None = None,
    exception: tuple[str, str] | None = None,
) -> TrialResult:
    """Build a real Harbor result suitable for lifecycle reporting."""
    config = TrialConfig(task=TaskConfig(path=Path(".")), trial_name=trial_name)
    return TrialResult(
        task_name="task",
        trial_name=trial_name,
        trial_uri=f"file:///tmp/{trial_name}",
        task_id=LocalTaskId(path=Path(".")),
        task_checksum="checksum",
        config=config,
        agent_info=AgentInfo(name="agent", version="1"),
        verifier_result=(
            VerifierResult(rewards={"reward": reward}) if reward is not None else None
        ),
        exception_info=(
            ExceptionInfo(
                exception_type=exception[0],
                exception_message=exception[1],
                exception_traceback="traceback",
                occurred_at=datetime.now(UTC),
            )
            if exception is not None
            else None
        ),
    )


def _use_queue(
    monkeypatch: pytest.MonkeyPatch,
    results: tuple[TrialResult, ...],
) -> _FakeTrialQueue:
    queue = _FakeTrialQueue(results)

    monkeypatch.setattr(runner, "TrialQueue", lambda **_: queue)
    return queue


def _metric_match(rendered: str, label: str, value: str) -> re.Match[str] | None:
    return re.search(
        rf"(?m)(?:^[ \t│]*|·[ \t]+){re.escape(label)}:?\s+{value}",
        rendered,
    )


def _displayed_number(rendered: str, label: str) -> float:
    match = _metric_match(rendered, label, r"\$?([\d,.]+)")
    if match is None:
        raise AssertionError(f"Missing displayed metric {label!r}")
    return float(match.group(1).replace(",", ""))


def _displayed_tokens(rendered: str, label: str) -> float:
    match = _metric_match(rendered, label, r"([\d,.]+)([KMB]?)")
    if match is None:
        raise AssertionError(f"Missing displayed token metric {label!r}")
    scale = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return float(match.group(1).replace(",", "")) * scale[match.group(2)]


def _assert_metric_order(rendered: str, labels: tuple[str, ...]) -> None:
    matches = [_metric_match(rendered, label, r"[^\n]+") for label in labels]
    assert all(match is not None for match in matches)
    positions = [match.start() for match in matches if match is not None]
    assert positions == sorted(positions)


def test_launch_trials_propagates_publication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed durable publication propagates without announcing completion."""
    result = _result("publication-failed", reward=1.0)
    _use_queue(monkeypatch, (result,))
    monkeypatch.setattr(runner, "logger", structlog.get_logger("harbor-runner-test"))

    async def publish(
        _value: TrialResult,
    ) -> tuple[TrialResult, TrialReport]:
        raise OSError("disk full")

    with capture_logs() as logs, pytest.raises(OSError, match="disk full"):
        asyncio.run(
            runner.launch_trials(
                (result.config,),
                run_dir=_RUN_DIR,
                n_concurrent=1,
                completion_processor=publish,
            )
        )

    assert not any(log["event"] == '"publication-failed" finished' for log in logs)


def test_launch_trials_combines_retained_and_new_completion_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed batch begins with finished cells in its live aggregates."""
    retained = _result("retained", reward=0.5)
    current = _result("current", reward=1.0)
    _use_queue(monkeypatch, (current,))
    output = StringIO()
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=80),
    )

    async def publish(value: TrialResult) -> tuple[TrialResult, TrialReport]:
        return value, _report(
            value.trial_name,
            simplicity=1.0,
            pass_rate=1.0,
            reward=1.0,
            input_tokens=750_000.0,
            output_tokens=75_000.0,
            api_cost=0.5,
            sandbox_cost=0.25,
        )

    asyncio.run(
        runner.launch_trials(
            (current.config,),
            run_dir=_RUN_DIR,
            n_concurrent=1,
            completion_processor=publish,
            retained_completions=(
                runner.RetainedTrialCompletion(
                    trial_name=retained.trial_name,
                    result=retained,
                    trial=_report(
                        retained.trial_name,
                        simplicity=0.5,
                        pass_rate=0.5,
                        reward=0.5,
                        input_tokens=250_000.0,
                        output_tokens=25_000.0,
                        api_cost=0.2,
                        sandbox_cost=0.05,
                    ),
                ),
            ),
        )
    )

    rendered = output.getvalue()
    assert "Finalized 2/2" in rendered
    assert _displayed_number(rendered, "Mean reward") == pytest.approx(0.75)
    assert _displayed_number(rendered, "simplicity") == pytest.approx(0.75)
    assert _displayed_number(rendered, "pass_rate") == pytest.approx(0.75)
    assert _displayed_tokens(rendered, "Tokens") == pytest.approx(1_100_000)
    assert _displayed_tokens(rendered, "output") == pytest.approx(100_000)
    assert _displayed_number(rendered, "Cost") == pytest.approx(1.0)
    assert _displayed_number(rendered, "API") == pytest.approx(0.7)
    assert _displayed_number(rendered, "sandbox") == pytest.approx(0.3)
    _assert_metric_order(
        rendered,
        (
            "Mean reward",
            "simplicity",
            "pass_rate",
            "Tokens",
            "output",
            "Cost",
            "API",
            "sandbox",
        ),
    )


def test_launch_trials_scores_incomplete_cells_as_zero_reward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete cell is a zero in mean reward and absent from the other means."""
    results = tuple(_result(name) for name in ("complete", "incomplete"))
    _use_queue(monkeypatch, results)
    output = StringIO()
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=80),
    )

    async def publish(value: TrialResult) -> tuple[TrialResult, TrialReport]:
        if value.trial_name == "incomplete":
            return value, _report(
                value.trial_name,
                reward=0.9,
                pass_rate=0.1,
                simplicity=0.1,
                incomplete_reason="VerifierTimeoutError: timed out",
            )
        return value, _report(
            value.trial_name, reward=0.8, pass_rate=1.0, simplicity=0.6
        )

    asyncio.run(
        runner.launch_trials(
            tuple(result.config for result in results),
            run_dir=_RUN_DIR,
            n_concurrent=1,
            completion_processor=publish,
        )
    )

    rendered = output.getvalue()
    assert _displayed_number(rendered, "Mean reward") == pytest.approx(0.4)
    assert _displayed_number(rendered, "pass_rate") == pytest.approx(1.0)
    assert _displayed_number(rendered, "simplicity") == pytest.approx(0.6)


def test_launch_trials_keeps_updating_known_costs_after_incomplete_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown incomplete spend does not hide later known cost evidence."""
    results = tuple(
        _result(name, exception=("AgentTimeoutError", "timed out"))
        for name in ("known-first", "unknown", "known-last")
    )
    _use_queue(monkeypatch, results)
    output = StringIO()
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=80),
    )
    costs = {
        "known-first": (0.4, 0.2),
        "unknown": (None, None),
        "known-last": (0.6, 0.3),
    }

    async def publish(value: TrialResult) -> tuple[TrialResult, TrialReport]:
        api_cost, sandbox_cost = costs[value.trial_name]
        return value, _report(
            value.trial_name,
            api_cost=api_cost,
            sandbox_cost=sandbox_cost,
            incomplete_reason="AgentTimeoutError: timed out",
        )

    asyncio.run(
        runner.launch_trials(
            tuple(result.config for result in results),
            run_dir=_RUN_DIR,
            n_concurrent=1,
            completion_processor=publish,
        )
    )

    rendered = output.getvalue()
    assert "Finalized 3/3" in rendered
    assert "Incomplete 3" in rendered
    assert _displayed_number(rendered, "API") == pytest.approx(1.0)
    assert _displayed_number(rendered, "sandbox") == pytest.approx(0.5)
    assert _displayed_number(rendered, "Cost") == pytest.approx(1.5)


def test_launch_trials_marks_unavailable_live_measurements_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing publication evidence is shown as unknown rather than zero."""
    result = _result("complete", reward=1.0)
    _use_queue(monkeypatch, (result,))
    output = StringIO()
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=80),
    )

    asyncio.run(
        runner.launch_trials(
            (result.config,),
            run_dir=_RUN_DIR,
            n_concurrent=1,
        )
    )

    rendered = output.getvalue()
    for label in (
        "Mean reward",
        "simplicity",
        "pass_rate",
        "Tokens",
        "output",
        "Cost",
        "API",
        "sandbox",
    ):
        assert _metric_match(rendered, label, "—") is not None


@pytest.mark.parametrize("width", [60, 200])
def test_launch_trials_shows_the_tail_of_the_run_directory(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    """The run's own leaf stays visible however narrow the terminal is."""
    result = _result("complete")
    _use_queue(monkeypatch, (result,))
    output = StringIO()
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=width),
    )

    asyncio.run(
        runner.launch_trials(
            (result.config,),
            run_dir=_RUN_DIR,
            n_concurrent=1,
        )
    )

    line = next(line for line in output.getvalue().splitlines() if "Saving to" in line)
    assert line.rstrip(" │").endswith(_RUN_DIR.name)
    assert ("…" in line) == (width < len(_RUN_DIR.as_posix()) + 20)


def test_launch_trials_names_each_model_setup_above_the_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every setup the batch runs gets its own header line."""
    result = _result("complete")
    _use_queue(monkeypatch, (result,))
    output = StringIO()
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=120),
    )
    setup = (
        "Model gpt-5.6-luna  ·  Reasoning high",
        "Model deepseek/deepseek-v4.1-flash  ·  Reasoning high",
    )

    asyncio.run(
        runner.launch_trials(
            (result.config,),
            run_dir=_RUN_DIR,
            setup=setup,
            n_concurrent=1,
        )
    )

    rendered = output.getvalue()
    positions = [rendered.find(line) for line in (*setup, "Finalized")]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)


def test_launch_trials_reports_active_and_failed_remote_spend_without_doubling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One failed remote attempt accrues live, settles once, and leaves metrics unknown."""
    raw = _result("remote-failure", exception=("AgentTimeoutError", "timed out"))
    environment = raw.config.environment.model_copy(
        update={"type": EnvironmentType.DAYTONA}
    )
    config = raw.config.model_copy(
        update={"environment": environment, "trials_dir": tmp_path}
    )
    result = raw.model_copy(update={"config": config})
    tracker = runner.SandboxCostTracker(tmp_path)

    class RemoteQueue:
        sandbox_costs = tracker

        def __init__(self) -> None:
            self.hooks: dict[TrialEvent, HookCallback] = {}

        def add_hook(self, event: TrialEvent, callback: HookCallback) -> None:
            self.hooks[event] = callback

        def submit_batch(
            self, configs: list[TrialConfig]
        ) -> list[Awaitable[TrialResult]]:
            assert configs == [config]

            async def execute() -> TrialResult:
                trial_dir = tmp_path / config.trial_name
                trial_dir.mkdir()
                resources = SimpleNamespace(cpu=2, memory=4, disk=10)
                trial = cast(
                    object,
                    SimpleNamespace(
                        config=config,
                        task=SimpleNamespace(
                            config=SimpleNamespace(
                                steps=None,
                                verifier=SimpleNamespace(
                                    environment_mode=None, environment=None
                                ),
                            )
                        ),
                        agent_environment=SimpleNamespace(
                            _sandbox_resources=lambda: resources,
                            _effective_gpus=0,
                        ),
                        paths=SimpleNamespace(trial_dir=trial_dir),
                    ),
                )
                started = datetime.now(UTC) - timedelta(hours=1)
                event = cast(
                    TrialHookEvent,
                    SimpleNamespace(
                        result=result,
                        config=config,
                        timestamp=started,
                    ),
                )
                await self.hooks[TrialEvent.START](event)
                execution = tracker.start(cast(Any, trial), event)
                await self.hooks[TrialEvent.ENVIRONMENT_START](event)
                tracker.finish(
                    cast(Any, trial),
                    cast(
                        TrialHookEvent,
                        SimpleNamespace(
                            result=result,
                            config=config,
                            timestamp=started + timedelta(hours=1),
                        ),
                    ),
                    execution,
                )
                return result

            return [execute()]

    renders: list[str] = []

    class RecordingLive:
        def __init__(self, renderable: object, **_kwargs: object) -> None:
            self.update(renderable, refresh=True)

        def __enter__(self) -> RecordingLive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def update(self, renderable: object, *, refresh: bool) -> None:
            del refresh
            output = StringIO()
            Console(file=output, width=80).print(renderable)
            renders.append(output.getvalue())

    queue = RemoteQueue()
    monkeypatch.setattr(runner, "TrialQueue", lambda **_: queue)
    monkeypatch.setattr(runner, "Live", RecordingLive)

    async def publish(value: TrialResult) -> tuple[TrialResult, TrialReport]:
        usage = load_sandbox_usage(
            tmp_path / config.trial_name,
            "daytona",
            tmp_path / "sandbox-usage" / f"{config.trial_name}.json",
        )
        return value, _report(
            value.trial_name,
            simplicity=0.0,
            sandbox_cost=usage.cost_usd,
            incomplete_reason="AgentTimeoutError: timed out",
        )

    asyncio.run(
        runner.launch_trials(
            (config,),
            run_dir=tmp_path,
            n_concurrent=1,
            completion_processor=publish,
        )
    )

    active = next(
        rendered
        for rendered in renders
        if "Active 1" in rendered
        and _metric_match(rendered, "sandbox", r"\$?([\d,.]+)") is not None
    )
    final = renders[-1]
    expected = 2 * 0.0504 + 4 * 0.0162 + 5 * 0.000108
    assert _displayed_number(active, "sandbox") == pytest.approx(expected, abs=0.001)
    assert _displayed_number(final, "sandbox") == pytest.approx(expected, abs=0.001)
    assert _displayed_number(final, "Cost") == pytest.approx(expected, abs=0.001)
    assert "Active 0" in final
    assert "Incomplete 1" in final
    assert _displayed_number(final, "Mean reward") == pytest.approx(0.0)
    assert _metric_match(final, "simplicity", "—") is not None


@pytest.mark.parametrize(
    ("exception_type", "failures", "executions"),
    [
        ("EnvironmentStartTimeoutError", 1, 2),
        ("EnvironmentStartTimeoutError", 9, 4),
        ("AgentTimeoutError", 1, 1),
        ("CancelledError", 1, 1),
    ],
)
def test_launch_retries_environment_failures_and_retains_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: str,
    failures: int,
    executions: int,
) -> None:
    """The real queue retries execution failures with bounded, retained attempts."""
    attempts: list[TrialResult] = []
    published: list[TrialResult] = []
    config = _result("environment-retry").config.model_copy(
        update={"trials_dir": tmp_path}
    )
    trial_dir = tmp_path / config.trial_name

    async def create_trial(config: TrialConfig) -> object:
        # Substitute remote trial execution while keeping queue policy and
        # LDB's retention/publication hooks real.
        result = _result(
            config.trial_name,
            exception=(exception_type, "provider failure")
            if len(attempts) < failures
            else None,
        )
        result.config = config
        attempts.append(result)
        trial_dir.mkdir()
        result_path = trial_dir / "result.json"
        hooks: dict[TrialEvent, list[HookCallback]] = {
            event: [] for event in TrialEvent
        }

        async def run() -> TrialResult:
            result_path.write_text(result.model_dump_json())
            event = cast(
                TrialHookEvent,
                SimpleNamespace(
                    result=result, config=config, trial_name=config.trial_name
                ),
            )
            for hook in hooks[TrialEvent.END]:
                await hook(event)
            return result

        return SimpleNamespace(
            config=config,
            result=result,
            paths=SimpleNamespace(trial_dir=trial_dir, result_path=result_path),
            run=run,
            _run_agent_phase=run,
            add_hook=lambda event, hook: hooks[event].append(hook),
        )

    async def publish(result: TrialResult) -> tuple[TrialResult, TrialReport]:
        published.append(result)
        return result, _report(result.trial_name)

    monkeypatch.setattr(runner.Trial, "create", create_trial)
    monkeypatch.setattr(
        runner.HarborTrialQueue, "_calculate_backoff_delay_sec", lambda *_: 0
    )
    launched = runner.launch_trials(
        (config,),
        run_dir=tmp_path,
        n_concurrent=1,
        completion_processor=publish,
    )
    if exception_type == "CancelledError":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(launched)
        assert len(attempts) == 1
        assert published == []
        assert not trial_dir.exists()
        return
    returned = asyncio.run(launched)
    assert len(attempts) == executions
    assert returned == (attempts[-1],)
    assert published == [attempts[-1]]
    assert (returned[0].exception_info is None) == (executions > failures)
    for execution in range(1, executions):
        previous = TrialResult.model_validate_json(
            (trial_dir / "retries" / str(execution) / "result.json").read_text()
        )
        assert previous.exception_info is not None
        assert previous.exception_info.exception_type == exception_type
    assert not (tmp_path / f".retries-{config.trial_name}").exists()


@pytest.mark.parametrize(
    ("failures", "executions"),
    [
        pytest.param(1, 2, id="recovers"),
        pytest.param(4, 4, id="exhausts"),
    ],
)
def test_launch_retries_agent_daytona_error_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
    executions: int,
) -> None:
    """An agent infrastructure failure retries even when verification returns a score."""
    attempts: list[TrialResult] = []
    published: list[TrialResult] = []
    config = _result("agent-retry").config.model_copy(update={"trials_dir": tmp_path})
    trial_dir = tmp_path / config.trial_name
    output = StringIO()

    async def create_trial(config: TrialConfig) -> object:
        result = _result(config.trial_name, reward=0.5)
        result.config = config
        attempts.append(result)
        trial_dir.mkdir()
        result_path = trial_dir / "result.json"
        hooks: dict[TrialEvent, list[HookCallback]] = {
            event: [] for event in TrialEvent
        }

        async def run_agent() -> None:
            if len(attempts) <= failures:
                raise DaytonaError("provider failure")

        async def run() -> TrialResult:
            await trial._run_agent_phase()
            event = cast(
                TrialHookEvent,
                SimpleNamespace(
                    result=result, config=config, trial_name=config.trial_name
                ),
            )
            for hook in hooks[TrialEvent.END]:
                await hook(event)
            return result

        trial = SimpleNamespace(
            config=config,
            result=result,
            paths=SimpleNamespace(trial_dir=trial_dir, result_path=result_path),
            run=run,
            _run_agent_phase=run_agent,
            add_hook=lambda event, hook: hooks[event].append(hook),
        )
        return trial

    async def publish(result: TrialResult) -> tuple[TrialResult, TrialReport]:
        published.append(result)
        return result, _report(
            result.trial_name,
            simplicity=0.5,
            reward=0.5,
            incomplete_reason=None
            if result.exception_info is None
            else result.exception_info.exception_type,
        )

    monkeypatch.setattr(runner.Trial, "create", create_trial)
    monkeypatch.setattr(
        runner.HarborTrialQueue, "_calculate_backoff_delay_sec", lambda *_: 0
    )
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=80),
    )

    (returned,) = asyncio.run(
        runner.launch_trials(
            (config,),
            run_dir=tmp_path,
            n_concurrent=1,
            completion_processor=publish,
        )
    )

    assert len(attempts) == executions
    assert published == [returned]
    if failures < executions:
        assert returned.exception_info is None
        assert _displayed_number(output.getvalue(), "simplicity") == pytest.approx(0.5)
        return
    assert returned.exception_info is not None
    assert returned.exception_info.exception_type == "DaytonaError"
    rendered = output.getvalue()
    assert "Incomplete 1" in rendered
    assert re.search(r"simplicity\s+—", rendered)
    assert re.search(r"pass_rate\s+—", rendered)
    for execution in range(1, executions):
        retried = trial_dir / "retries" / str(execution)
        assert (retried / "agent-error.json").is_file()
        previous = TrialResult.model_validate_json(
            (retried / "result.json").read_text()
        )
        assert previous.exception_info is not None
        assert previous.exception_info.exception_type == "DaytonaError"


def test_launch_scores_a_spent_usage_limit_without_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spent usage limit ends the trial with its partial work scored."""
    attempts: list[TrialResult] = []
    published: list[TrialResult] = []
    config = _result("limit-spent").config.model_copy(update={"trials_dir": tmp_path})
    trial_dir = tmp_path / config.trial_name

    async def create_trial(config: TrialConfig) -> object:
        result = _result(config.trial_name, reward=0.5)
        result.config = config
        attempts.append(result)
        trial_dir.mkdir()
        result_path = trial_dir / "result.json"
        hooks: dict[TrialEvent, list[HookCallback]] = {
            event: [] for event in TrialEvent
        }

        async def run_agent() -> None:
            raise ApiUsageLimitError("You've hit your usage limit")

        async def run() -> TrialResult:
            await trial._run_agent_phase()
            event = cast(
                TrialHookEvent,
                SimpleNamespace(
                    result=result, config=config, trial_name=config.trial_name
                ),
            )
            for hook in hooks[TrialEvent.END]:
                await hook(event)
            return result

        trial = SimpleNamespace(
            config=config,
            result=result,
            paths=SimpleNamespace(trial_dir=trial_dir, result_path=result_path),
            run=run,
            _run_agent_phase=run_agent,
            add_hook=lambda event, hook: hooks[event].append(hook),
        )
        return trial

    async def publish(result: TrialResult) -> tuple[TrialResult, TrialReport]:
        published.append(result)
        return result, _report(result.trial_name, reward=0.5)

    monkeypatch.setattr(runner.Trial, "create", create_trial)
    monkeypatch.setattr(
        runner.HarborTrialQueue, "_calculate_backoff_delay_sec", lambda *_: 0
    )

    (returned,) = asyncio.run(
        runner.launch_trials(
            (config,),
            run_dir=tmp_path,
            n_concurrent=1,
            completion_processor=publish,
        )
    )

    assert len(attempts) == 1
    assert published == [returned]
    assert returned.verifier_result is not None
    assert returned.exception_info is not None
    assert returned.exception_info.exception_type == "ApiUsageLimitError"
    assert returned.exception_info.exception_message == "You've hit your usage limit"
    assert not (trial_dir / "retries").exists()
    assert (trial_dir / "agent-error.json").is_file()
    limit = LimitRecord.from_json(trial_dir / LIMIT_RECORD_NAME)
    assert limit.kind == "cost"
    assert limit.detail == "You've hit your usage limit"
    persisted = TrialResult.model_validate_json((trial_dir / "result.json").read_text())
    assert persisted.exception_info is not None
    assert persisted.exception_info.exception_type == "ApiUsageLimitError"
