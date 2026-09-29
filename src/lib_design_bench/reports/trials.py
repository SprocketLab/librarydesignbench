"""Project one Harbor trial result into its persisted LDB trial report."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse

import structlog
from harbor.models.agent.context import AgentContext
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.result import TimingInfo
from harbor.models.trial.result import TrialResult
from harbor.models.verifier.result import VerifierResult

from lib_design_bench.metrics.costs import persisted_cost_rates
from lib_design_bench.metrics.costs import standardize_usage_cost
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.manifest import DesignLaunch
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import AttemptStatus
from lib_design_bench.models.reports import LibraryKind
from lib_design_bench.models.reports import SandboxUsageReport
from lib_design_bench.models.reports import StaticMetrics
from lib_design_bench.models.reports import TrialIssue
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.models.reports import UsageReport
from lib_design_bench.runs.store import TRIAL_REPORT_FILE_NAME
from lib_design_bench.runs.store import TRIAL_RESULT_FILE_NAME
from lib_design_bench.runs.store import OutputDocument
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import load_sandbox_usage
from lib_design_bench.runs.store import sandbox_ledger_path
from lib_design_bench.runs.store import source_run_dir
from lib_design_bench.runs.store import verifier_reward
from lib_design_bench.runs.store import verifier_rewards
from lib_design_bench.runs.store import write_run_output_documents

logger = structlog.get_logger(__name__)


def trial_uri_path(trial_uri: str) -> Path | None:
    """Resolve a `file://` trial URI to a local path."""
    parsed = urlparse(trial_uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def library_kind(launch: TrialLaunch) -> LibraryKind:
    """Return the library kind one planned launch reports."""
    match launch:
        case DesignLaunch():
            return "author"
        case EvaluationLaunch(arm=arm):
            match arm.condition:
                case NoLibrary():
                    return "no-library"
                case AuthoredArtifact():
                    return "agent"
                case ExistingLibrary():
                    return "existing"


@dataclass(frozen=True)
class TrialOutcome:
    """Verifier, reward, usage, and error evidence for one trial."""

    result: TrialResult
    reward: float | None
    verifier_rewards: dict[str, float]
    usage: UsageReport
    had_error: bool
    error_reason: str | None


def trial_outcome_from_result(result: TrialResult, trial_dir: Path) -> TrialOutcome:
    """Project Harbor verifier, reward, usage, and error evidence."""
    trajectory = _trajectory_data(trial_dir / "agent" / "trajectory.json")
    error_reason = trial_error_reason(result)
    return TrialOutcome(
        result=result,
        reward=verifier_reward(result),
        verifier_rewards=_verifier_rewards(result),
        usage=_usage_report(result, trajectory),
        had_error=error_reason is not None,
        error_reason=error_reason,
    )


def trial_error_reason(result: TrialResult) -> str | None:
    """Return concise trial exception details, if any."""
    issue = result.exception_info
    if issue is None:
        return None
    return f"{issue.exception_type}: {issue.exception_message}"


def recover_completed_verifier(result: TrialResult, trial_dir: Path) -> TrialResult:
    """Recover Harbor's downloaded reward when an exception interrupted capture."""
    if result.verifier_result is not None:
        return result
    reward_path = trial_dir / "verifier" / "reward.json"
    if not reward_path.is_file():
        return result
    rewards = json.loads(reward_path.read_text(encoding="utf-8"))
    return result.model_copy(
        update={
            "verifier_result": VerifierResult(rewards=rewards),
        }
    )


def attempt_status(has_issues: bool, pass_rate: float | None) -> AttemptStatus:
    """Classify a persisted attempt from its verifier outcome."""
    if has_issues:
        return "failed"
    if pass_rate == 1.0:
        return "passed"
    if pass_rate is not None:
        return "completed"
    return "unknown"


def trial_issues(result: TrialResult) -> tuple[TrialIssue, ...]:
    """Return issues surfaced by one Harbor result."""
    issues: list[TrialIssue] = []
    if result.exception_info is not None:
        issues.append(
            TrialIssue(
                trial_name=result.trial_name,
                issue=result.exception_info.exception_type,
                detail=result.exception_info.exception_message,
                reward=verifier_reward(result),
                trial_uri=result.trial_uri,
                exception_info=result.exception_info,
            )
        )

    if result.exception_info is None:
        reward = verifier_reward(result)
        if reward is None:
            issues.append(
                TrialIssue(
                    trial_name=result.trial_name,
                    issue="missing reward",
                    detail="Verifier result did not include `reward`.",
                    reward=None,
                    trial_uri=result.trial_uri,
                )
            )
    return tuple(issues)


def _verifier_rewards(result: TrialResult) -> dict[str, float]:
    return {
        name: float(value)
        for name, value in verifier_rewards(result).items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _usage_report(result: TrialResult, data: Mapping[str, Any]) -> UsageReport:
    timings = [result.agent_execution, result.verifier]
    durations = tuple(
        value for timing in timings if (value := _duration_seconds(timing)) is not None
    )
    trajectory = _trajectory_metrics(data)
    agent = _agent_usage(result.agent_result)
    trajectory_has_complete_input = all(
        name in trajectory
        for name in ("input_tokens", "uncached_input_tokens", "cache_input_tokens")
    )
    agent_has_complete_input = all(
        name in agent
        for name in ("input_tokens", "uncached_input_tokens", "cache_input_tokens")
    )
    if agent_has_complete_input:
        input_usage = {
            name: agent[name]
            for name in ("input_tokens", "uncached_input_tokens", "cache_input_tokens")
        }
    elif trajectory_has_complete_input:
        input_usage = {
            name: trajectory[name]
            for name in ("input_tokens", "uncached_input_tokens", "cache_input_tokens")
        }
    else:
        input_fields = ("input_tokens", "uncached_input_tokens", "cache_input_tokens")
        source = next(
            (
                candidate
                for candidate in (agent, trajectory)
                if any(candidate.get(name) is not None for name in input_fields)
            ),
            {},
        )
        input_usage = {
            name: value
            for name in input_fields
            for value in (source.get(name),)
            if value is not None
        }
    independent = {
        name: value
        for source in (trajectory, agent)
        for name, value in source.items()
        if name not in {"input_tokens", "uncached_input_tokens", "cache_input_tokens"}
    }
    return UsageReport.model_validate(
        {
            **independent,
            **input_usage,
            "time_spent": sum(durations) if durations else None,
        }
    )


def _agent_usage(agent: AgentContext | None) -> dict[str, int | float]:
    if agent is None:
        return {}
    cache_tokens = agent.n_cache_tokens
    input_tokens = agent.n_input_tokens
    if (
        input_tokens is not None
        and cache_tokens is not None
        and cache_tokens > input_tokens
    ):
        raise ValueError("Agent cached-input tokens exceed total input tokens")
    uncached_input_tokens = (
        None
        if input_tokens is None or cache_tokens is None
        else input_tokens - cache_tokens
    )
    net_tokens = (
        None
        if agent.n_input_tokens is None and agent.n_output_tokens is None
        else (agent.n_input_tokens or 0) + (agent.n_output_tokens or 0)
    )
    return {
        name: value
        for name, value in {
            "net_token_usage": net_tokens,
            "last_token_usage": agent.n_input_tokens,
            "input_tokens": input_tokens,
            "uncached_input_tokens": uncached_input_tokens,
            "cache_input_tokens": cache_tokens,
            "output_tokens": agent.n_output_tokens,
            "cost_usd": agent.cost_usd,
        }.items()
        if value is not None
    }


def _duration_seconds(timing: TimingInfo | None) -> float | None:
    if timing is None or timing.started_at is None or timing.finished_at is None:
        return None
    return (timing.finished_at - timing.started_at).total_seconds()


def _trajectory_data(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}.")
    return data


def _trajectory_metrics(data: Mapping[str, Any]) -> dict[str, float]:
    final_metrics = data.get("final_metrics")
    if not isinstance(final_metrics, dict):
        return {}
    numeric = {
        name: float(value)
        for name in (
            "total_prompt_tokens",
            "total_cached_tokens",
            "total_completion_tokens",
            "total_cost_usd",
            "total_steps",
        )
        for value in (final_metrics.get(name),)
        if isinstance(value, int | float) and not isinstance(value, bool)
    }
    prompt = numeric.get("total_prompt_tokens")
    cached = numeric.get("total_cached_tokens")
    if prompt is not None and cached is not None and cached > prompt:
        raise ValueError("Trajectory cached-input tokens exceed total input tokens")
    return {
        key: value
        for key, value in {
            "input_tokens": prompt,
            "uncached_input_tokens": (
                None if prompt is None or cached is None else prompt - cached
            ),
            "cache_input_tokens": cached,
            "output_tokens": numeric.get("total_completion_tokens"),
            "cost_usd": numeric.get("total_cost_usd"),
            "agent_steps": numeric.get("total_steps"),
        }.items()
        if value is not None
    }


def build_trial_report(
    launch: TrialLaunch,
    result: TrialResult | None,
    sandbox_usage: SandboxUsageReport | None = None,
) -> TrialReport:
    """Project Harbor verifier numbers without measuring or recomputing scores."""
    is_evaluation = isinstance(launch, EvaluationLaunch)
    reference = launch.problem.static_reference() if is_evaluation else None
    condition = launch.arm.condition if is_evaluation else None
    environment = launch.environment if result is None else result.config.environment
    environment_type = "unknown" if environment.type is None else environment.type.value
    common = {
        "schema_version": 1,
        "trial_name": launch.trial_name,
        "implementor": launch.arm.label if is_evaluation else "author",
        "task": launch.task.name,
        "problem": launch.problem.name if is_evaluation else "design",
        "library": launch.library_name,
        **agent_identity(launch.agent),
        "attempt": launch.attempt,
        "design_attempt": (
            condition.attempt if isinstance(condition, AuthoredArtifact) else None
        ),
        "source_design_run": _source_design_run(condition),
        "prompt_path": (
            None if (path := launch.prompt_relpath()) is None else path.as_posix()
        ),
        "environment_type": environment_type,
        "library_kind": library_kind(launch),
        "reference_library": reference.library if reference is not None else None,
    }
    if result is None:
        return TrialReport.model_validate(
            {
                **common,
                "trial_path": launch.trial_name,
                "trial_uri": f"file:///{launch.trial_name}",
                "reward": 0.0,
                "pass_rate": 0.0 if is_evaluation else None,
                "status": "failed",
                "simplicity": 0.0 if is_evaluation else None,
                "score": 0.0,
                "sandbox_usage": sandbox_usage
                or SandboxUsageReport(
                    cost_usd=0.0 if environment_type == "docker" else None
                ),
                "incomplete_reason": (
                    launch.incomplete_reason if is_evaluation else None
                )
                or "missing Harbor result",
            }
        )
    trial_dir = trial_uri_path(result.trial_uri) or Path(result.trial_name)
    outcome = trial_outcome_from_result(result, trial_dir)
    sandbox_usage = sandbox_usage or load_sandbox_usage(
        trial_dir,
        environment_type,
        sandbox_ledger_path(result.config.trials_dir, result.trial_name),
    )
    rewards = outcome.verifier_rewards
    pass_rate = rewards.get("pass_rate") if is_evaluation else None
    metrics = None
    if is_evaluation and all(name in rewards for name in StaticMetrics.scalar_names()):
        # Harbor may serialize integral metric values as floats.
        metrics = StaticMetrics.model_validate(
            {
                name: rewards[name]
                if name == "halstead_volume"
                else _count(rewards[name])
                for name in StaticMetrics.scalar_names()
            }
        )
    reason = outcome.error_reason
    if is_evaluation and metrics is None:
        reason = reason or "verifier measurement unavailable; see verifier logs"
    if is_evaluation and (pass_rate is None or "simplicity" not in rewards):
        reason = reason or "missing verifier measurement contract"
    if outcome.reward is None:
        reason = reason or "missing verifier reward"
    return TrialReport.model_validate(
        {
            **common,
            "trial_path": trial_dir.as_posix(),
            "trial_uri": outcome.result.trial_uri,
            "reward": outcome.reward,
            "pass_rate": pass_rate,
            "verifier_rewards": rewards,
            "usage": outcome.usage,
            "sandbox_usage": sandbox_usage,
            "had_error": outcome.had_error,
            "status": attempt_status(
                outcome.had_error, pass_rate if is_evaluation else outcome.reward
            ),
            "incomplete_reason": reason,
            "static_analysis": metrics,
            "simplicity": rewards.get("simplicity", 0.0) if is_evaluation else None,
            "score": outcome.reward or 0.0,
            "simplicity_ratios": {
                key.removeprefix("ratio."): value
                for key, value in rewards.items()
                if key.startswith("ratio.")
            }
            if is_evaluation
            else {},
        }
    )


def _source_design_run(condition: object | None) -> str | None:
    """Return canonical authored provenance, absent for controls or uncollected artifacts."""
    if not isinstance(condition, AuthoredArtifact):
        return None
    source = source_run_dir(condition.source)
    return None if source is None else source.as_posix()


def _count(value: float) -> int:
    if not value.is_integer():
        raise ValueError(f"Verifier metric count must be integral: {value}")
    return int(value)


def agent_identity(agent: AgentConfig | None) -> dict[str, str]:
    """Return the agent, model, and reasoning names one report records."""
    if agent is None:
        return {"agent": "unknown", "model": "unknown", "reasoning": "none"}
    return {
        "agent": agent.name or agent.import_path or "unknown",
        "model": agent.model_name or "unknown",
        "reasoning": str(agent.kwargs.get("reasoning_effort", "none")),
    }


@dataclass
class TrialPublisher:
    """Persist Harbor evidence and derived reports for finished trial results."""

    run: Run
    launches: Mapping[str, TrialLaunch]
    previous_usage: Mapping[str, UsageReport]

    async def publish(self, result: TrialResult) -> tuple[TrialResult, TrialReport]:
        """Persist one final trial's evidence and score before completion logging."""
        launch = self.launches[result.trial_name]
        trial_dir = trial_uri_path(result.trial_uri) or Path(result.trial_name)
        destination = self.run.slot(launch).dir
        if trial_dir != destination:
            shutil.move(trial_dir, destination)
            result = result.model_copy(
                update={
                    "trial_uri": destination.resolve().as_uri(),
                    "config": result.config.model_copy(
                        update={"trials_dir": self.run.dir}
                    ),
                }
            )
            trial_dir = destination
        result = recover_completed_verifier(result, trial_dir)
        trial = build_trial_report(launch, result)
        prior = self.previous_usage.get(result.trial_name)
        rates = persisted_cost_rates(prior)
        if rates is not None:
            trial = trial.model_copy(
                update={"usage": standardize_usage_cost(trial.usage, rates)}
            )
        write_run_output_documents(
            (
                OutputDocument(
                    trial_dir / TRIAL_RESULT_FILE_NAME, result.model_dump_json(indent=4)
                ),
                OutputDocument(
                    trial_dir / TRIAL_REPORT_FILE_NAME,
                    trial.model_dump_json(indent=2) + "\n",
                ),
            )
        )
        logger.debug(
            "Published trial evidence.",
            trial_name=result.trial_name,
            run_kind=launch.kind,
            static_available=trial.static_analysis is not None,
            output_dir=destination.as_posix(),
            score=trial.score,
        )
        return result, trial
