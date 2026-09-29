"""Estimate and persist external sandbox compute spend."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from harbor.models.task.verifier_mode import task_has_any_separate_verifier
from harbor.trial.hooks import TrialHookEvent
from harbor.trial.trial import Trial

from lib_design_bench.models.reports import SandboxAttempt
from lib_design_bench.models.reports import SandboxUsageReport
from lib_design_bench.runs.store import SANDBOX_USAGE_FILE_NAME
from lib_design_bench.runs.store import OutputDocument
from lib_design_bench.runs.store import sandbox_ledger_path
from lib_design_bench.runs.store import write_run_output_documents

DAYTONA_PRICING_SOURCE = "https://www.daytona.io/pricing (retrieved 2026-09-15)"


MODAL_PRICING_SOURCE = "https://modal.com/pricing (retrieved 2026-09-15)"


_DAYTONA_CPU_PER_HOUR = 0.0504


_DAYTONA_MEMORY_GIB_PER_HOUR = 0.0162


_DAYTONA_STORAGE_GIB_PER_HOUR = 0.000108


_DAYTONA_FREE_STORAGE_GIB = 5.0


_MODAL_CPU_CORE_PER_SECOND = 0.00003942


_MODAL_MEMORY_GIB_PER_SECOND = 0.00000667


_MODAL_DEFAULT_CPU_CORES = 0.125


_MODAL_DEFAULT_MEMORY_MIB = 128.0


class SandboxCostTracker:
    """Track every physical remote attempt owned by one Harbor queue."""

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self._lock = Lock()
        self._active: dict[tuple[str, int], tuple[datetime, SandboxAttempt]] = {}
        self._attempts: dict[str, list[SandboxAttempt]] = {}

    def start(self, trial: Trial, event: TrialHookEvent) -> int | None:
        """Persist an open provider interval before Harbor starts the sandbox."""
        name = trial.config.trial_name
        with self._lock:
            attempts = self._attempts_for(name)
            execution = max((attempt.execution for attempt in attempts), default=0) + 1
            attempt = _attempt_template(trial, event.timestamp, execution)
            if attempt is None:
                return None
            attempts.append(attempt)
            self._active[(name, execution)] = (event.timestamp, attempt)
            self._write_usage(trial, _usage_report(tuple(attempts)))
            return execution

    def finish(
        self, trial: Trial, event: TrialHookEvent, execution: int | None
    ) -> None:
        """Finish, price, and persist an attempt after sandbox teardown."""
        if execution is None:
            return
        name = trial.config.trial_name
        with self._lock:
            active = self._active.pop((name, execution), None)
            if active is None:
                return
            started_at, template = active
            attempt = _finish_attempt(template, started_at, event.timestamp)
            attempts = self._attempts_for(name)
            index = next(
                index
                for index, existing in enumerate(attempts)
                if existing.execution == execution
            )
            attempts[index] = attempt
            self._write_usage(trial, _usage_report(tuple(attempts)))

    def _attempts_for(self, trial_name: str) -> list[SandboxAttempt]:
        attempts = self._attempts.get(trial_name)
        if attempts is not None:
            return attempts
        path = sandbox_ledger_path(self._run_dir, trial_name)
        attempts = (
            []
            if not path.is_file()
            else list(
                SandboxUsageReport.model_validate_json(
                    path.read_text(encoding="utf-8")
                ).attempts
            )
        )
        self._attempts[trial_name] = attempts
        return attempts

    def _write_usage(self, trial: Trial, report: SandboxUsageReport) -> None:
        document = report.model_dump_json(indent=2) + "\n"
        write_run_output_documents(
            (
                OutputDocument(
                    trial.paths.trial_dir / SANDBOX_USAGE_FILE_NAME, document
                ),
                OutputDocument(
                    sandbox_ledger_path(self._run_dir, trial.config.trial_name),
                    document,
                ),
            )
        )

    def live_cost_usd(
        self, *, excluding: set[str], now: datetime
    ) -> tuple[bool, float | None]:
        """Snapshot whether unreported usage exists and its current estimate."""
        with self._lock:
            relevant: list[SandboxAttempt] = []
            active_keys = set(self._active)
            for trial_name, attempts in self._attempts.items():
                if trial_name not in excluding:
                    relevant.extend(
                        attempt
                        for attempt in attempts
                        if (trial_name, attempt.execution) not in active_keys
                    )
            for (trial_name, _execution), (
                started_at,
                template,
            ) in self._active.items():
                if trial_name not in excluding:
                    relevant.append(_finish_attempt(template, started_at, now))
            return bool(relevant), _attempt_cost_sum(relevant)


def _attempt_template(
    trial: Trial, started_at: datetime, execution: int
) -> SandboxAttempt | None:
    environment_type = trial.config.environment.type
    name = None if environment_type is None else environment_type.value
    environment: Any = trial.agent_environment
    scope_complete = _scope_complete(trial)
    if name == "daytona":
        resources = environment._sandbox_resources()
        return SandboxAttempt(
            environment_type="daytona",
            execution=execution,
            started_at=started_at,
            finished_at=None,
            duration_seconds=None,
            cpus=_float_or_none(None if resources is None else resources.cpu),
            memory_gib=_float_or_none(None if resources is None else resources.memory),
            storage_gib=_float_or_none(None if resources is None else resources.disk),
            gpus=int(getattr(environment, "_effective_gpus", 0)),
            cost_usd=None,
            cpu_rate_usd=_DAYTONA_CPU_PER_HOUR,
            memory_rate_usd=_DAYTONA_MEMORY_GIB_PER_HOUR,
            storage_rate_usd=_DAYTONA_STORAGE_GIB_PER_HOUR,
            free_storage_gib=_DAYTONA_FREE_STORAGE_GIB,
            pricing_unit="hour",
            estimate_kind="reserved",
            scope_complete=scope_complete,
            pricing_source=DAYTONA_PRICING_SOURCE,
            exclusions=(
                "GPU",
                "network",
                "snapshots",
                "image builds",
                *(("separate verifier sandbox",) if not scope_complete else ()),
            ),
        )
    if name == "docker":
        return None
    if name != "modal":
        return SandboxAttempt(
            environment_type=name or "unknown",
            execution=execution,
            started_at=started_at,
            cpus=_float_or_none(environment._effective_cpus),
            memory_gib=(
                None
                if environment._effective_memory_mb is None
                else float(environment._effective_memory_mb) / 1024
            ),
            storage_gib=(
                None
                if environment._effective_storage_mb is None
                else float(environment._effective_storage_mb) / 1024
            ),
            gpus=int(getattr(environment, "_effective_gpus", 0)),
            estimate_kind="unknown",
            scope_complete=scope_complete,
            pricing_source="unavailable",
            exclusions=("unsupported environment pricing",),
        )
    cpu_config = environment._cpu_config()
    memory_config = environment._memory_config()
    cpus = _request_value(cpu_config, _MODAL_DEFAULT_CPU_CORES)
    memory_mib = _request_value(memory_config, _MODAL_DEFAULT_MEMORY_MIB)
    return SandboxAttempt(
        environment_type="modal",
        execution=execution,
        started_at=started_at,
        finished_at=None,
        duration_seconds=None,
        cpus=cpus,
        memory_gib=memory_mib / 1024,
        storage_gib=None,
        gpus=int(getattr(environment, "_effective_gpus", 0)),
        cost_usd=None,
        cpu_rate_usd=_MODAL_CPU_CORE_PER_SECOND,
        memory_rate_usd=_MODAL_MEMORY_GIB_PER_SECOND,
        storage_rate_usd=None,
        pricing_unit="second",
        estimate_kind="request-estimate",
        scope_complete=scope_complete,
        pricing_source=MODAL_PRICING_SOURCE,
        exclusions=(
            "burst usage",
            "GPU",
            "network",
            "image builds",
            *(("separate verifier sandbox",) if not scope_complete else ()),
        ),
    )


def _finish_attempt(
    template: SandboxAttempt, started_at: datetime, finished_at: datetime
) -> SandboxAttempt:
    seconds = max((finished_at - started_at).total_seconds(), 0.0)
    cost = _estimate_attempt_cost(template, seconds)
    return template.model_copy(
        update={
            "finished_at": finished_at,
            "duration_seconds": seconds,
            "cost_usd": cost,
        }
    )


def _estimate_attempt_cost(attempt: SandboxAttempt, seconds: float) -> float | None:
    if (
        not attempt.scope_complete
        or attempt.gpus
        or attempt.cpus is None
        or attempt.memory_gib is None
        or attempt.cpu_rate_usd is None
        or attempt.memory_rate_usd is None
        or attempt.pricing_unit is None
    ):
        return None
    resource_rate = (
        attempt.cpus * attempt.cpu_rate_usd
        + attempt.memory_gib * attempt.memory_rate_usd
    )
    if attempt.pricing_unit == "second":
        return seconds * resource_rate
    if attempt.storage_gib is None or attempt.storage_rate_usd is None:
        return None
    hourly = (
        resource_rate
        + max(attempt.storage_gib - attempt.free_storage_gib, 0.0)
        * attempt.storage_rate_usd
    )
    return seconds * hourly / 3600


def _usage_report(attempts: tuple[SandboxAttempt, ...]) -> SandboxUsageReport:
    return SandboxUsageReport(
        cost_usd=_attempt_cost_sum(attempts),
        attempts=attempts,
    )


def _attempt_cost_sum(
    attempts: list[SandboxAttempt] | tuple[SandboxAttempt, ...],
) -> float | None:
    if not attempts:
        return 0.0
    if any(attempt.cost_usd is None for attempt in attempts):
        return None
    return sum(attempt.cost_usd or 0.0 for attempt in attempts)


def _scope_complete(trial: Trial) -> bool:
    task = getattr(trial, "task", None)
    if task is None:
        return True
    return not task_has_any_separate_verifier(task.config)


def _request_value(
    value: int | float | tuple[int | float, int] | None, default: float
) -> float:
    if value is None:
        return default
    if isinstance(value, tuple):
        value = value[0]
    return float(value)


def _float_or_none(value: int | float | None) -> float | None:
    return None if value is None else float(value)
