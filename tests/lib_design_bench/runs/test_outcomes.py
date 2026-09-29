"""Outcome classification decides what resumption spends on a saved slot.

A misclassified slot either burns a whole agent run on evidence that only
needed regrading, or leaves a broken trial in the report as if it had
finished. Every case here is driven by a checked-in Harbor result.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lib_design_bench.models.reports import MEASURE_FAILURE_LOG
from lib_design_bench.runs.outcomes import classify
from lib_design_bench.runs.store import Slot
from tests.lib_design_bench.conftest import seed_slot_artifacts
from tests.lib_design_bench.conftest import seed_slot_trajectory


def _slot(fixtures_root: Path, tmp_path: Path, fixture: str) -> Slot:
    """Copy one outcome fixture into a slot named for the trial it holds."""
    source = fixtures_root / "outcomes" / fixture
    slot = Slot(dir=tmp_path / fixture)
    shutil.copytree(source, slot.dir)
    return slot


def _set_rewards(slot: Slot, rewards: dict[str, float]) -> Slot:
    """Rewrite the persisted verifier rewards of a copied fixture."""
    path = slot.dir / "result.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["verifier_result"]["rewards"] = rewards
    path.write_text(json.dumps(document), encoding="utf-8")
    return slot


def test_timeout_without_a_collected_trajectory_is_rerun(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """A timeout with no trajectory cannot show the agent ever worked."""
    assert classify(_slot(fixtures_root, tmp_path, "agent_timeout")).outcome == "rerun"


def test_timeout_without_a_model_turn_is_rerun(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """An agent that never reached the model spent its hour on nothing."""
    slot = _slot(fixtures_root, tmp_path, "agent_timeout")
    seed_slot_trajectory(slot, "system", "user")
    seed_slot_artifacts(slot)

    assert classify(slot).outcome == "rerun"


def test_timeout_after_a_model_turn_is_finished(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """An agent that worked until the clock ran out produced its own answer."""
    slot = _slot(fixtures_root, tmp_path, "agent_timeout")
    seed_slot_trajectory(slot, "system", "agent")
    seed_slot_artifacts(slot)

    assert classify(slot).outcome == "finished"


def test_measurement_that_already_failed_is_finished(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """A recorded measurement failure is settled evidence, not endless reanalysis.

    Reanalyzing source the formatter or analyzer cannot read produces the same
    missing scalars forever, so the stage log the verifier left behind ends it.
    """
    slot = _set_rewards(
        _slot(fixtures_root, tmp_path, "clean"),
        {"reward": 1.0, "pass_rate": 1.0, "simplicity": 0.0, "passed": 1, "total": 1},
    )
    seed_slot_artifacts(slot)
    (slot.dir / "verifier").mkdir()
    (slot.dir / "verifier" / MEASURE_FAILURE_LOG).write_text(
        "Traceback\n", encoding="utf-8"
    )

    assert classify(slot).outcome == "finished"


def test_missing_static_metrics_without_workspace_are_finished(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """A graded trial is never respent to recover a static number it lost."""
    slot = _set_rewards(
        _slot(fixtures_root, tmp_path, "clean"),
        {
            "reward": 1.0,
            "pass_rate": 0.95,
            "simplicity": 0.0,
            "passed": 19,
            "total": 20,
        },
    )

    assert classify(slot).outcome == "finished"


def test_reward_over_zero_tests_is_reverified(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """A verifier that never reached its suite graded nothing to observe."""
    slot = _set_rewards(
        _slot(fixtures_root, tmp_path, "clean"),
        {"reward": 0.0, "pass_rate": 0.0, "simplicity": 0.5, "passed": 0, "total": 0},
    )
    seed_slot_artifacts(slot)

    assert classify(slot).outcome == "reverify"


def test_reward_over_zero_tests_without_artifacts_is_rerun(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """Nothing survived to regrade, so the whole trial has to run again."""
    slot = _set_rewards(
        _slot(fixtures_root, tmp_path, "clean"),
        {"reward": 0.0, "pass_rate": 0.0, "simplicity": 0.5, "passed": 0, "total": 0},
    )

    assert classify(slot).outcome == "rerun"


def test_verifier_timeout_is_reverify(fixtures_root: Path, tmp_path: Path) -> None:
    """The agent's work survived, so only its grading needs replaying."""
    slot = _slot(fixtures_root, tmp_path, "verifier_timeout")
    seed_slot_artifacts(slot)

    assert classify(slot).outcome == "reverify"


def test_auth_error_is_rerun(fixtures_root: Path, tmp_path: Path) -> None:
    """A credential fault produced no usable trial."""
    assert classify(_slot(fixtures_root, tmp_path, "auth_error")).outcome == "rerun"


_CLAUDE_RESULT = (
    '{"type":"system","subtype":"init","model":"claude-fable-5-1"}\n'
    '{"type":"assistant","message":{"model":"claude-fable-5-1"}}\n'
)


def _claude_log(slot: Slot, result_line: str | None) -> Slot:
    """Give a copied fixture a Claude Code stream-json log ending as given."""
    text = _CLAUDE_RESULT + ("" if result_line is None else result_line + "\n")
    (slot.dir / "agent").mkdir(exist_ok=True)
    (slot.dir / "agent" / "claude-code.txt").write_text(text, encoding="utf-8")
    return slot


def test_claude_code_error_result_is_rerun(fixtures_root: Path, tmp_path: Path) -> None:
    """Claude Code exits zero after a usage limit; only its result event says so."""
    slot = _claude_log(
        _slot(fixtures_root, tmp_path, "clean"),
        '{"type":"result","subtype":"error_during_execution","is_error":true,'
        '"result":"You\'ve hit your limit"}',
    )

    decision = classify(slot)

    assert decision.outcome == "rerun"
    assert "error_during_execution" in decision.reason


def test_claude_code_log_without_a_result_event_is_rerun(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """A stream-json log that never reached its result event was cut off."""
    slot = _claude_log(_slot(fixtures_root, tmp_path, "clean"), None)

    assert classify(slot).outcome == "rerun"


@pytest.mark.parametrize("subtype", ["success", "error_max_budget"])
def test_claude_code_success_and_limit_results_are_finished(
    fixtures_root: Path, tmp_path: Path, subtype: str
) -> None:
    """A finished session, or one stopped at its budget limit, is evidence."""
    slot = _claude_log(
        _slot(fixtures_root, tmp_path, "clean"),
        f'{{"type":"result","subtype":"{subtype}","is_error":false}}',
    )

    assert classify(slot).outcome == "finished"


def test_missing_result_is_rerun(tmp_path: Path) -> None:
    """A slot without its persisted result cannot be reported."""
    slot = Slot(dir=tmp_path / "missing_result")
    slot.dir.mkdir()
    (slot.dir / "trial.log").write_text("trial did not persist a result\n")

    assert classify(slot).outcome == "rerun"


def test_mismatched_trial_name_is_rerun(fixtures_root: Path, tmp_path: Path) -> None:
    """A result that belongs to another trial cannot be reported."""
    assert (
        classify(_slot(fixtures_root, tmp_path, "mismatched_trial_name")).outcome
        == "rerun"
    )


_STALLED_AGENT_LOG = "\n".join(
    (
        '{"type":"item.completed","item":{"id":"item_0","type":"error",'
        '"message":"Falling back from WebSockets to HTTPS transport. stream '
        'disconnected before completion: tls handshake eof"}}',
        '{"type":"error","message":"Reconnecting... waiting for network '
        '(Connection failed: error sending request)"}',
        "2026-09-09T20:31:52.680194Z ERROR codex_models_manager::manager: failed to"
        " refresh available models: timeout waiting for child process to exit",
        '{"type":"error","message":"Reconnecting... waiting for network '
        '(Connection failed: error sending request)"}',
    )
)
"""How codex's log ends once its sandbox can no longer reach the provider."""


_WORKING_AGENT_LOG = "\n".join(
    (
        '{"type":"error","message":"Reconnecting... waiting for network '
        '(Connection failed: error sending request)"}',
        '{"type":"item.started","item":{"id":"item_9","type":"command_execution",'
        '"command":"cargo test"}}',
        '{"type":"item.completed","item":{"id":"item_9","type":"command_execution",'
        '"exit_code":0}}',
    )
)
"""A log whose agent recovered from a reconnect and kept working."""


def test_timeout_spent_waiting_for_network_is_rerun(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """An agent that idled on a dead sandbox network produced no answer to keep.

    The trajectory and workspace here would otherwise settle the timeout, so
    only the dead network can decide this slot.
    """
    slot = _slot(fixtures_root, tmp_path, "agent_timeout")
    seed_slot_trajectory(slot, "system", "agent")
    seed_slot_artifacts(slot)
    (slot.dir / "agent" / "codex.txt").write_text(_STALLED_AGENT_LOG, encoding="utf-8")

    assert classify(slot).outcome == "rerun"


def test_timeout_after_recovered_reconnect_stays_finished(
    fixtures_root: Path, tmp_path: Path
) -> None:
    """A reconnect the agent worked past does not turn its spent hour into a fault."""
    slot = _slot(fixtures_root, tmp_path, "agent_timeout")
    seed_slot_trajectory(slot, "system", "agent")
    seed_slot_artifacts(slot)
    (slot.dir / "agent" / "codex.txt").write_text(_WORKING_AGENT_LOG, encoding="utf-8")

    assert classify(slot).outcome == "finished"
