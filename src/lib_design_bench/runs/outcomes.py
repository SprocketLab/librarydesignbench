"""The outcome class of one persisted trial slot, and what resumption owes it."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

import structlog
from harbor.models.trial.result import TrialResult

from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import AGENT_LIMIT_EXCEPTIONS
from lib_design_bench.models.reports import FORMAT_FAILURE_LOG
from lib_design_bench.models.reports import MEASURE_FAILURE_LOG
from lib_design_bench.models.reports import OutcomeClass
from lib_design_bench.models.reports import StaticMetrics
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import Slot
from lib_design_bench.runs.store import verifier_rewards

logger = structlog.get_logger(__name__)


CLAUDE_CODE_LOG_NAME = "claude-code.txt"
"""Claude Code's stream-json log; its final `result` event names how it ended."""


CLAUDE_CODE_LIMIT_SUBTYPES = frozenset({"error_max_turns", "error_max_budget"})
"""Result subtypes that mean the agent stopped at a configured limit, not a fault."""


VERIFIER_EXCEPTIONS = frozenset(
    {
        # harbor.trial.errors
        "VerifierTimeoutError",
        # harbor.verifier.verifier
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
        "AddTestsDirError",
        "DownloadVerifierDirError",
    }
)
"""Harbor's verification-phase failures, which leave the agent's work intact.

The agent finished and its workspace was collected; only the grading of it
failed, so replaying the verifier over the saved artifacts recovers the trial
without spending the agent again.
"""


@dataclass(frozen=True)
class Classification:
    """One slot's outcome class and the persisted evidence that decided it."""

    outcome: OutcomeClass
    reason: str


def classify(slot: Slot) -> Classification:
    """Return what resumption owes one persisted slot.

    `finished` is an observable, fair outcome: a graded result with no
    operational failure, a spent cost limit, or a timeout the agent spent
    working. A limit is read from the trial's limit record or, when that names
    none, from an exception in `AGENT_LIMIT_EXCEPTIONS`.

    `reverify` means the agent's work survived and only its grading did not, so
    replaying the verifier over the retained workspace recovers the trial.
    A reward counting no tests is such a failure: the verifier ran but graded
    nothing. `reanalyze` means grading succeeded while static measurement was
    never attempted, so the retained workspace is measured without another
    agent.

    `rerun` is everything else, including a timeout that took no model turn or
    idled on a dead provider network: an agent that never worked produced no
    outcome to keep.
    """
    result = slot.result()
    if result is None:
        return Classification("rerun", "no valid result names this slot")
    limit = slot.limit()
    failures = _failures(result)
    for failure in failures:
        if failure in VERIFIER_EXCEPTIONS:
            return _regrade(slot, f"verification raised {failure}")
    kind = "none" if limit is None else limit.kind
    if kind == "none":
        for failure in failures:
            if failure not in AGENT_LIMIT_EXCEPTIONS:
                return Classification("rerun", f"the trial raised {failure}")
        if failures:
            kind = AGENT_LIMIT_EXCEPTIONS[failures[0]]
    if kind == "timeout":
        if network_stalled(_agent_log_tail(slot)):
            return Classification(
                "rerun", "the agent timed out while its network was down"
            )
        if not _took_a_turn(slot):
            return Classification("rerun", "the agent timed out without taking a turn")
    if kind == "none" and (agent_failure := _claude_code_failure(slot)) is not None:
        return Classification("rerun", agent_failure)
    if _graded_no_tests(result):
        return _regrade(slot, "the verifier graded no tests")
    if _never_measured(slot, result):
        return Classification("reanalyze", "static measurement was never attempted")
    if kind != "none":
        return Classification("finished", f"the agent stopped at its {kind} limit")
    return Classification("finished", "a complete result without an exception")


def launches_in(run: Run, outcome: OutcomeClass) -> tuple[TrialLaunch, ...]:
    """Return the planned trials of one run whose slots are in `outcome`."""
    selected = []
    for launch in run.launches():
        slot = run.slot(launch)
        classification = classify(slot)
        if classification.outcome != outcome:
            continue
        selected.append(launch)
        # A slot that never started has no evidence to explain.
        if slot.dir.exists():
            logger.debug(
                "Selected a persisted trial slot.",
                slot=slot.dir.as_posix(),
                outcome=outcome,
                reason=classification.reason,
            )
    return tuple(selected)


def classify_run(run: Run) -> dict[str, OutcomeClass]:
    """Return the outcome class of every planned slot of one run, by trial name."""
    return {
        launch.trial_name: classify(run.slot(launch)).outcome
        for launch in run.launches()
    }


def _failures(result: TrialResult) -> tuple[str, ...]:
    """Return the trial exception type when Harbor recorded one."""
    return (
        () if result.exception_info is None else (result.exception_info.exception_type,)
    )


def _regrade(slot: Slot, reason: str) -> Classification:
    """Replay the verifier when the agent's collected work is still readable."""
    error = slot.workspace_error()
    if error is None:
        return Classification("reverify", reason)
    return Classification("rerun", f"{reason} without artifacts to replay: {error}")


def _graded_no_tests(result: TrialResult) -> bool:
    """Return whether the verifier produced a reward over zero behavioral tests.

    A zero pass rate out of zero tests grades nothing: the verifier itself
    failed to reach the suite, so the trial has no behavioral outcome yet.
    """
    return verifier_rewards(result).get("total") == 0


def _never_measured(slot: Slot, result: TrialResult) -> bool:
    """Return whether a graded slot still has an unattempted static measurement.

    A measurement that ran and failed leaves its stage log beside the reward,
    and produces the same missing scalars every time it runs again, so only an
    unattempted measurement over a retained workspace is recoverable work.
    """
    rewards = verifier_rewards(result)
    if "pass_rate" not in rewards:
        return False
    if "simplicity" in rewards and all(
        name in rewards for name in StaticMetrics.scalar_names()
    ):
        return False
    attempted = any(
        (slot.dir / "verifier" / name).is_file()
        for name in (FORMAT_FAILURE_LOG, MEASURE_FAILURE_LOG)
    )
    return not attempted and slot.workspace_error() is None


def _took_a_turn(slot: Slot) -> bool:
    """Return whether the collected trajectory records at least one model turn.

    A trajectory holding only the system prompt and the task never reached the
    model, so the hour the agent spent bought nothing to grade.
    """
    path = slot.dir / "agent" / "trajectory.json"
    if not path.is_file():
        return False
    document = json.loads(path.read_text(encoding="utf-8"))
    steps = document.get("steps", ()) if isinstance(document, dict) else ()
    return any(step.get("source") == "agent" for step in steps)


def _claude_code_failure(slot: Slot) -> str | None:
    """Return why a collected Claude Code session did not finish its work.

    Claude Code exits zero after a usage limit, an authentication failure, or
    an internal error, leaving Harbor a clean-looking trial over an empty or
    half-written workspace. Its stream-json log ends with one `result` event
    whose subtype tells the two apart; a log without that event was cut off.
    """
    path = slot.dir / "agent" / CLAUDE_CODE_LOG_NAME
    if not path.is_file():
        return None
    result: dict[str, object] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    if result is None:
        return "the Claude Code log ended without a result event"
    subtype = str(result.get("subtype") or "success")
    if subtype in CLAUDE_CODE_LIMIT_SUBTYPES:
        return None
    if subtype != "success" or result.get("is_error"):
        detail = str(result.get("result") or result.get("error") or "")[:120]
        return f"Claude Code reported {subtype}: {detail}".rstrip(": ")
    return None


def _agent_log_tail(slot: Slot) -> tuple[str, ...]:
    """Return the trailing lines of the slot's agent log, if one was collected."""
    path = slot.dir / "agent" / AGENT_LOG_NAME
    if not path.is_file():
        return ()
    return tuple(path.read_text(encoding="utf-8").splitlines()[-STALL_WINDOW_LINES:])


AGENT_LOG_NAME = "codex.txt"
"""The agent log under Harbor's agent directory, as the codex adapter names it."""


NETWORK_STALL_MESSAGE = "waiting for network"
"""The reconnect message codex repeats while the provider is unreachable."""


STALL_WINDOW_LINES = 10
"""How many trailing log lines must show no progress to call the agent stalled."""


def network_stalled(lines: Iterable[str]) -> bool:
    """Return whether the log's recent lines show only reconnect failures.

    Codex writes one JSON event per line plus raw tracing lines. Any recent
    event that is not an error, or a completed item that is not an error,
    proves the agent is still working. A window without the reconnect
    message is a healthy or merely quiet agent.
    """
    recent = [line for line in lines if line.strip()][-STALL_WINDOW_LINES:]
    saw_reconnect = False
    for line in recent:
        if NETWORK_STALL_MESSAGE in line:
            saw_reconnect = True
        elif line.startswith("{") and _is_progress(line):
            return False
    return saw_reconnect


def _is_progress(line: str) -> bool:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict):
        return False
    item = event.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    return event.get("type") != "error" and item_type != "error"
