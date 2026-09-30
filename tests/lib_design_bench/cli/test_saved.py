"""`ldb resume` and `ldb recalculate` rework a saved run in place."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Coroutine
from datetime import UTC
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from harbor.models.agent.context import AgentContext
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import TaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import AgentInfo
from harbor.models.trial.result import ExceptionInfo
from harbor.models.trial.result import TrialResult
from harbor.models.verifier.result import VerifierResult
from rich.console import Console
from typer.testing import CliRunner

from lib_design_bench.cli import app
from lib_design_bench.harbor import runner
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import FORMAT_FAILURE_LOG
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.models.reports import UsageReport
from lib_design_bench.models.task import Problem
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.pipeline.run import lease
from lib_design_bench.pipeline.run import run
from lib_design_bench.reports.rebuild import persisted_report
from lib_design_bench.runs.outcomes import classify
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import Slot
from tests.lib_design_bench.conftest import MINIMAL_CONFIG
from tests.lib_design_bench.conftest import Outcome
from tests.lib_design_bench.conftest import RecordingQueue
from tests.lib_design_bench.conftest import evaluation_cells
from tests.lib_design_bench.conftest import experiment_dir
from tests.lib_design_bench.conftest import invoke_experiment
from tests.lib_design_bench.conftest import seed_slot_artifacts
from tests.lib_design_bench.conftest import seed_slot_failure
from tests.lib_design_bench.conftest import seed_workspace_manifest


def _evaluation_run(tasks_root: Path, run_dir: Path) -> tuple[TrialLaunch, ...]:
    """Execute one two-cell no-library run, so resume has real slots to read."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=task.active_problems(),
        arms=(
            Arm(label="impl", condition=NoLibrary(), agent=AgentConfig(name="oracle")),
        ),
        n_concurrent=1,
    )
    with lease(run_dir, force=False):
        launches = plan(request, run_dir)
        run(launches, run_dir, n_concurrent=1, debug_build_contexts=False)
        finalize(run_dir)
    return launches


def _slots(run_dir: Path, launches: tuple[TrialLaunch, ...]) -> tuple[Slot, ...]:
    persisted = Run.open(run_dir)
    return tuple(persisted.slot(launch) for launch in launches)


def _drop_static_metrics(slot: Slot) -> None:
    """Leave a graded slot the behavioral rewards but none of its measurement."""
    result = slot.result()
    if result is None or result.verifier_result is None:
        raise ValueError("Fixture run must produce a verifier result")
    rewards = result.verifier_result.rewards
    if rewards is None:
        raise ValueError("Fixture run must produce verifier rewards")
    behavioral = {
        name: rewards[name] for name in ("reward", "pass_rate", "passed", "total")
    }
    (slot.dir / "result.json").write_text(
        result.model_copy(
            update={
                "verifier_result": result.verifier_result.model_copy(
                    update={"rewards": behavioral}
                )
            }
        ).model_dump_json(),
        encoding="utf-8",
    )


def _replay_failure(exception_type: str, message: str) -> Outcome:
    """End a replayed trial with one Harbor exception and no graded reward."""
    return Outcome(
        reward=None,
        exception=ExceptionInfo(
            exception_type=exception_type,
            exception_message=message,
            exception_traceback=f"{exception_type}: {message}\n",
            occurred_at=datetime.now(UTC),
        ),
    )


def _ungraded_run(
    tasks_root: Path, fixtures_root: Path, run_dir: Path
) -> tuple[Slot, ...]:
    """Run two cells, leaving the first slot's grading failed but replayable."""
    launches = _evaluation_run(tasks_root, run_dir)
    slots = _slots(run_dir, launches)
    seed_slot_failure(slots[0], fixtures_root, "verifier_error")
    seed_slot_artifacts(slots[0])
    return slots


async def _on_running_loop(
    loops: list[asyncio.AbstractEventLoop], pending: Coroutine[Any, Any, TrialResult]
) -> TrialResult:
    """Execute one trial, recording the event loop that carried it."""
    loops.append(asyncio.get_running_loop())
    return await pending


def _resume(run_dir: Path) -> None:
    result = CliRunner().invoke(app, ["resume", str(run_dir)])
    assert result.exit_code == 0, result.output


def test_resume_switches_environment_and_keeps_it_for_the_next_resume(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A saved Docker run reruns its broken slot on Modal and stays on Modal."""
    run_dir = tmp_path / "run"
    launches = _evaluation_run(tasks_root, run_dir)
    broken, _kept = _slots(run_dir, launches)
    saved = Run.open(run_dir).request().environment
    assert saved.type == EnvironmentType.DOCKER
    for options in (["environment.type=modal"], []):
        seed_slot_failure(broken, fixtures_root, "provider_error")
        recording_queue.batches.clear()

        result = CliRunner().invoke(app, ["resume", str(run_dir), *options])

        assert result.exit_code == 0, result.output
        assert recording_queue.trial_names == (broken.dir.name,)
        (config,) = recording_queue.configs
        assert config.environment.type == EnvironmentType.MODAL
        persisted = Run.open(run_dir).request().environment
        assert persisted.type == EnvironmentType.MODAL
        assert persisted.override_cpus == saved.override_cpus
        assert persisted.override_memory_mb == saved.override_memory_mb


def test_resume_reverifies_verifier_error_slots_in_place_and_stamps_verification_record(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A failed grading is replayed over saved artifacts and merged back in place."""
    run_dir = tmp_path / "run"
    ungraded, _ = _ungraded_run(tasks_root, fixtures_root, run_dir)
    (ungraded.dir / "artifacts" / "container-python").symlink_to("container-python")
    recording_queue.batches.clear()

    _resume(run_dir)

    assert recording_queue.trial_names == (ungraded.dir.name,)
    replayed = AgentConfig.model_validate(
        recording_queue.configs[0].agent.kwargs["inner_agent"]
    )
    assert replayed.model_name == "artifact-replay"
    assert replayed.kwargs["artifacts_dir"] == str(ungraded.dir / "artifacts")
    result = ungraded.result()
    assert result is not None
    assert result.exception_info is None
    assert ungraded.reward() == 1.0
    verification = Run.open(run_dir).manifest.verification
    assert verification is not None
    assert tuple(trial.path for trial in verification.source_trials) == (ungraded.dir,)


def test_resume_reverify_live_progress_includes_finished_slots(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifier replay shows the scores of cells that did not need replaying."""
    run_dir = tmp_path / "run"
    ungraded, _ = _ungraded_run(tasks_root, fixtures_root, run_dir)
    (ungraded.dir / "artifacts" / "container-python").symlink_to("container-python")
    output = StringIO()
    monkeypatch.setattr(
        runner,
        "get_rich_console",
        lambda: Console(file=output, width=80),
    )

    _resume(run_dir)

    assert "Finalized 2/2" in output.getvalue()


def test_resume_reanalyzes_missing_static_metrics_without_rerunning_agent(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A behavioral result with retained source is remeasured by a skip-tests replay."""
    run_dir = tmp_path / "run"
    launches = _evaluation_run(tasks_root, run_dir)
    unmeasured, retained = _slots(run_dir, launches)
    _drop_static_metrics(unmeasured)
    assert classify(unmeasured).outcome == "reanalyze"
    recording_queue.batches.clear()

    _resume(run_dir)

    (replayed,) = recording_queue.configs
    assert replayed.trial_name == unmeasured.dir.name
    assert replayed.verifier.env == {
        "LDB_SKIP_TESTS": "1",
        "LDB_PASSED": "1",
        "LDB_TOTAL": "1",
    }
    refreshed = unmeasured.trial_report()
    assert refreshed is not None
    assert refreshed.incomplete_reason is None
    assert refreshed.static_analysis is not None
    assert retained.trial_report() is not None


def test_resume_settles_a_slot_whose_measurement_cannot_succeed(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Reanalysis that fails records why, so the next resume stops offering it.

    A workspace the formatter cannot read loses the same scalars every pass, so
    without the verifier's recorded stage failure the slot would ask to be
    reanalyzed forever and never reach an observable outcome.
    """
    run_dir = tmp_path / "run"
    launches = _evaluation_run(tasks_root, run_dir)
    unmeasurable, _ = _slots(run_dir, launches)
    _drop_static_metrics(unmeasurable)
    assert classify(unmeasurable).outcome == "reanalyze"
    recording_queue.batches.clear()
    recording_queue.outcomes[unmeasurable.dir.name] = Outcome(unmeasured=True)

    _resume(run_dir)
    _resume(run_dir)

    assert len(recording_queue.configs) == 1
    assert classify(unmeasurable).outcome == "finished"
    assert (unmeasurable.dir / "verifier" / FORMAT_FAILURE_LOG).is_file()
    settled = unmeasurable.trial_report()
    assert settled is not None
    assert settled.static_analysis is None
    assert settled.incomplete_reason is not None


def test_resume_recomputes_static_evidence_and_ldb_result_for_every_cell(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Rerunning one cell refreshes the whole run, not only the cell it touched."""
    run_dir = tmp_path / "run"
    launches = _evaluation_run(tasks_root, run_dir)
    broken, kept = _slots(run_dir, launches)
    seed_slot_failure(broken, fixtures_root, "auth_error")
    graded = kept.trial_report()
    assert graded is not None
    (kept.dir / "trial-report.json").write_text(
        TrialReport.model_copy(graded, update={"reward": 0.125}).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )

    _resume(run_dir)

    refreshed = kept.trial_report()
    assert refreshed is not None
    assert refreshed.reward == 1.0
    assert kept.trial_report() is not None
    assert broken.trial_report() is not None
    report = persisted_report(Run.open(run_dir))
    assert {attempt.trial_name: attempt.reward for attempt in report.attempts} == {
        broken.dir.name: 1.0,
        kept.dir.name: 1.0,
    }


def test_resume_keeps_source_evidence_when_the_replay_itself_fails(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A replay killed by its own environment never overwrites what it regraded.

    The replay observed nothing about the agent's saved work, so merging its
    infrastructure failure would destroy the artifacts and grading evidence the
    slot still needs and would ask resumption to buy the agent again.
    """
    run_dir = tmp_path / "run"
    ungraded, _ = _ungraded_run(tasks_root, fixtures_root, run_dir)
    graded_log = ungraded.dir / "verifier" / "verifier.log"
    graded_log.parent.mkdir(parents=True, exist_ok=True)
    graded_log.write_text("collected 12 items\n", encoding="utf-8")
    persisted = (ungraded.dir / "result.json").read_bytes()
    recording_queue.outcomes[ungraded.dir.name] = _replay_failure(
        "DaytonaError", "Failed to create snapshot: Event loop is closed"
    )
    recording_queue.batches.clear()

    _resume(run_dir)

    assert recording_queue.trial_names == (ungraded.dir.name,)
    assert (ungraded.dir / "result.json").read_bytes() == persisted
    assert graded_log.read_text(encoding="utf-8") == "collected 12 items\n"
    assert classify(ungraded).outcome == "reverify"
    assert Run.open(run_dir).manifest.verification is None


def test_resume_merges_a_replay_whose_verifier_failed_again(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Grading that reached the saved work and failed is the slot's new evidence."""
    run_dir = tmp_path / "run"
    ungraded, _ = _ungraded_run(tasks_root, fixtures_root, run_dir)
    recording_queue.outcomes[ungraded.dir.name] = _replay_failure(
        "VerifierTimeoutError", "Verifier timed out after 1800.0 seconds"
    )

    _resume(run_dir)

    merged = ungraded.result()
    assert merged is not None
    assert merged.exception_info is not None
    assert merged.exception_info.exception_type == "VerifierTimeoutError"
    verification = Run.open(run_dir).manifest.verification
    assert verification is not None
    assert tuple(trial.path for trial in verification.source_trials) == (ungraded.dir,)


def test_resume_reruns_and_replays_under_one_event_loop(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaying after a rerun keeps the loop the environment bound itself to.

    A remote environment caches a client and a lock on the loop that created
    them, so a rerun phase and a replay phase carried by separate loops fail
    every trial the second one is given.
    """
    run_dir = tmp_path / "run"
    relaunched, ungraded = _slots(run_dir, _evaluation_run(tasks_root, run_dir))
    seed_slot_failure(relaunched, fixtures_root, "provider_error")
    seed_slot_failure(ungraded, fixtures_root, "verifier_error")
    seed_slot_artifacts(ungraded)
    loops: list[asyncio.AbstractEventLoop] = []
    submit_batch = recording_queue.submit_batch
    monkeypatch.setattr(
        recording_queue,
        "submit_batch",
        lambda configs: [
            _on_running_loop(loops, pending) for pending in submit_batch(configs)
        ],
    )
    recording_queue.batches.clear()

    _resume(run_dir)

    assert recording_queue.trial_names == (relaunched.dir.name, ungraded.dir.name)
    assert len(set(loops)) == 1
    assert ungraded.reward() == 1.0


def test_resume_commits_replays_into_a_run_without_its_result_file(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A replay merge needs only the slots it regraded, not the run's result file.

    The run-level LDB scoreboard is removed whenever slot evidence changes
    and written again only when the reports are rebuilt.
    A resume that replays into a run in that state must still commit the
    replays it paid for.
    """
    run_dir = tmp_path / "run"
    (ungraded, _) = _ungraded_run(tasks_root, fixtures_root, run_dir)
    (run_dir / "ldb-result.json").unlink()
    recording_queue.batches.clear()

    _resume(run_dir)

    assert recording_queue.trial_names == (ungraded.dir.name,)
    assert ungraded.reward() == 1.0
    assert Run.open(run_dir).manifest.verification is not None
    assert (run_dir / "ldb-result.json").is_file()


def test_resume_regrade_preserves_trial_owned_historical_pricing(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Verifier replay changes scoring without replacing agent pricing evidence."""
    run_dir = tmp_path / "run"
    ungraded, _ = _ungraded_run(tasks_root, fixtures_root, run_dir)
    previous = ungraded.trial_report()
    assert previous is not None
    usage = UsageReport(
        input_tokens=2_000_000,
        uncached_input_tokens=1_500_000,
        cache_input_tokens=500_000,
        output_tokens=250_000,
        cost_usd=5.5,
        reported_cost_usd=0.75,
        standardized_cost_usd=5.5,
        input_cost_per_million=2,
        output_cost_per_million=8,
        cache_input_cost_per_million=1,
    )
    (ungraded.dir / "trial-report.json").write_text(
        previous.model_copy(update={"usage": usage}).model_dump_json()
    )

    _resume(run_dir)

    retained = ungraded.trial_report()
    assert retained is not None
    assert retained.usage == usage
    result = LdbResult.load(run_dir / "ldb-result.json")
    assert (
        next(trial for trial in result.trials if trial.name == ungraded.dir.name).cost
        == 5.5
    )
    verification = Run.open(run_dir).manifest.verification
    assert verification is not None
    assert result.meta.complete is True
    assert result.meta.finished_at is None


def test_rerun_inherits_rates_but_prices_its_new_agent_usage(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a failed slot cannot drop its historical pricing policy."""
    run_dir = tmp_path / "run"
    (broken, _) = _slots(run_dir, _evaluation_run(tasks_root, run_dir))
    old = broken.trial_report()
    assert old is not None
    usage = UsageReport(
        input_tokens=2_000_000,
        uncached_input_tokens=1_500_000,
        cache_input_tokens=500_000,
        output_tokens=250_000,
        cost_usd=5.5,
        reported_cost_usd=0.75,
        standardized_cost_usd=5.5,
        input_cost_per_million=2,
        output_cost_per_million=8,
        cache_input_cost_per_million=1,
    )
    (broken.dir / "trial-report.json").write_text(
        old.model_copy(update={"usage": usage}).model_dump_json()
    )
    seed_slot_failure(broken, fixtures_root, "provider_error")
    submit = recording_queue.submit_batch

    async def with_usage(pending: Coroutine[Any, Any, TrialResult]) -> TrialResult:
        result = await pending
        return result.model_copy(
            update={
                "agent_result": AgentContext(
                    n_input_tokens=3_000_000,
                    n_cache_tokens=500_000,
                    n_output_tokens=250_000,
                    cost_usd=0.9,
                )
            }
        )

    monkeypatch.setattr(
        recording_queue,
        "submit_batch",
        lambda configs: [with_usage(p) for p in submit(configs)],
    )
    _resume(run_dir)

    priced = broken.trial_report()
    assert priced is not None
    assert priced.usage.input_cost_per_million == 2
    assert priced.usage.reported_cost_usd == 0.9
    assert priced.usage.cost_usd == 7.5
    result = LdbResult.load(run_dir / "ldb-result.json")
    assert (
        next(trial for trial in result.trials if trial.name == broken.dir.name).cost
        == 7.5
    )


def test_json_resume_in_standalone_evaluation_results_directory(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Ownership follows the manifest, not a coincidental directory basename."""
    run_dir = tmp_path / "evaluation_results"
    _evaluation_run(tasks_root, run_dir)
    returned = CliRunner().invoke(app, ["resume", str(run_dir), "--json"])
    assert returned.exit_code == 0, returned.output
    result = LdbResult.model_validate(json.loads(returned.stdout))
    assert result.id == "evaluation_results"
    assert result.meta.type == "evaluation"
    assert result == LdbResult.load(run_dir / "ldb-result.json")


def test_rerun_without_new_usage_does_not_reuse_the_previous_attempts_cost(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """The old rate policy survives, but an unmeasured new agent has no cost."""
    run_dir = tmp_path / "run"
    (broken, _) = _slots(run_dir, _evaluation_run(tasks_root, run_dir))
    old = broken.trial_report()
    assert old is not None
    (broken.dir / "trial-report.json").write_text(
        old.model_copy(
            update={
                "usage": UsageReport(
                    input_tokens=2_000_000,
                    uncached_input_tokens=1_500_000,
                    cache_input_tokens=500_000,
                    output_tokens=250_000,
                    cost_usd=5.5,
                    reported_cost_usd=0.75,
                    standardized_cost_usd=5.5,
                    input_cost_per_million=2,
                    output_cost_per_million=8,
                    cache_input_cost_per_million=1,
                )
            }
        ).model_dump_json()
    )
    seed_slot_failure(broken, fixtures_root, "provider_error")
    recording_queue.batches.clear()

    _resume(run_dir)

    assert recording_queue.trial_names == (broken.dir.name,)
    replaced = broken.trial_report()
    assert replaced is not None
    assert replaced.usage.input_cost_per_million == 2
    assert replaced.usage.cost_usd is None
    assert not tuple(run_dir.glob(".harbor-*"))
    selected = [
        entry
        for line in (run_dir / "run.log").read_text().splitlines()
        if (entry := json.loads(line))["event"] == "Selected a persisted trial slot."
    ]
    assert [(entry["slot"], entry["outcome"]) for entry in selected] == [
        (broken.dir.as_posix(), "rerun")
    ]
    assert selected[0]["reason"]
    published = LdbResult.load(run_dir / "ldb-result.json")
    assert (
        next(trial for trial in published.trials if trial.name == broken.dir.name).cost
        is None
    )


def test_verify_run_json_stdout_is_only_the_replay_result(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Replay reads the original agent but emits one parseable result."""
    source = tmp_path / "source"
    _evaluation_run(tasks_root, source)
    output = tmp_path / "replay"
    returned = CliRunner().invoke(
        app, ["verify", "run", str(source), "--output", str(output), "--json"]
    )
    assert returned.exit_code == 0, returned.output
    result = LdbResult.model_validate(json.loads(returned.stdout))
    (saved,) = tuple(output.rglob("ldb-result.json"))
    assert result == LdbResult.load(saved)
    assert result.meta.type == "replay"


def _graded_slot(
    run_dir: Path, problem: Problem, *, exception: ExceptionInfo | None = None
) -> Path:
    """Plan a one-cell no-library run and fill its slot with a graded solution.

    A remeasure replays the collected workspace with the counts the persisted
    Harbor result recorded, so both must be on disk before the command runs.
    """
    launch = plan(
        Job(
            problems=(problem,),
            arms=(
                Arm(
                    label="impl",
                    condition=NoLibrary(),
                    agent=AgentConfig(name="oracle"),
                ),
            ),
            n_concurrent=1,
        ),
        run_dir,
    )[0]
    trial_dir = Run.open(run_dir).slot(launch).dir
    shutil.copytree(
        problem.solution_dir("no-library"), trial_dir / "artifacts" / "workspace"
    )
    seed_workspace_manifest(Slot(trial_dir))
    result = TrialResult(
        task_name="fixture-task",
        trial_name=launch.trial_name,
        trial_uri=trial_dir.as_uri(),
        task_id=LocalTaskId(path=trial_dir),
        task_checksum="fixture-checksum",
        config=TrialConfig(
            task=TaskConfig(path=trial_dir), trial_name=launch.trial_name
        ),
        agent_info=AgentInfo(name="oracle", version="fixture"),
        verifier_result=VerifierResult(
            rewards={
                "reward": 1.0,
                "pass_rate": 1.0,
                "simplicity": 1.0,
                "passed": 1,
                "total": 1,
            }
        ),
        exception_info=exception,
    )
    (trial_dir / "result.json").write_text(result.model_dump_json(indent=2))
    (trial_dir / "verifier").mkdir()
    (trial_dir / "verifier" / "pytest.stdout").write_text("2 passed\n")
    return trial_dir


@pytest.mark.parametrize("failed", [False, True])
def test_recalculate_remeasures_a_typed_run_and_keeps_its_agent_evidence(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue, failed: bool
) -> None:
    """A remeasure replaces the reward and keeps what the agent's run recorded.

    The replay skips the behavioral tests, so the slot's own test logs and
    the exception that ended its agent both outlive the new measurement.
    """
    run_dir = tmp_path / "run"
    trial_dir = _graded_slot(
        run_dir,
        Task.from_dir(tasks_root / "pyt").problem("01_step"),
        exception=(
            ExceptionInfo.from_exception(RuntimeError("agent stopped"))
            if failed
            else None
        ),
    )

    completed = CliRunner().invoke(app, ["recalculate", run_dir.as_posix()])

    assert completed.exit_code == 0, completed.output
    (replayed,) = recording_queue.configs
    assert replayed.verifier.env["LDB_SKIP_TESTS"] == "1"
    rewards = json.loads((trial_dir / "verifier" / "reward.json").read_text())
    run_report = persisted_report(Run.open(run_dir))
    trial_report = run_report.attempts[0]
    native = TrialResult.model_validate_json((trial_dir / "result.json").read_text())
    assert native.verifier_result is not None
    assert native.verifier_result.rewards == rewards
    assert (trial_dir / "verifier" / "pytest.stdout").read_text() == "2 passed\n"
    assert trial_report.static_analysis is not None
    assert trial_report.reward == trial_report.score == rewards["reward"] == 1.0
    assert trial_report.pass_rate == 1.0
    saved = json.loads((run_dir / "ldb-result.json").read_text())
    assert saved["schema_version"] == 1
    assert saved["trials"][0]["pass_rate"] == 1.0
    assert trial_report.had_error is failed


def test_recalculate_keeps_a_slot_whose_saved_counts_are_invalid(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """A remeasure never fabricates counts, so malformed evidence is not replayed."""
    run_dir = tmp_path / "run"
    trial_dir = _graded_slot(
        run_dir, Task.from_dir(tasks_root / "pyt").problem("01_step")
    )
    result_path = trial_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["verifier_result"]["rewards"].update(passed=2, total=1)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    completed = CliRunner().invoke(app, ["recalculate", run_dir.as_posix()])

    assert completed.exit_code == 0, completed.output
    assert recording_queue.configs == ()
    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert (
        saved["verifier_result"]["rewards"]["passed"],
        saved["verifier_result"]["rewards"]["total"],
    ) == (2, 1)


def test_recalculate_remeasures_direct_evaluation_children_of_a_design_run(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """Remeasuring a design root remeasures its direct authored evaluation."""
    task = Task.from_dir(tasks_root / "pyt")
    design_dir = tmp_path / "design"
    plan(
        AuthorJob(
            tasks=(task,),
            agent=AgentConfig(name="oracle"),
            n_concurrent=1,
        ),
        design_dir,
    )
    trial_dir = _graded_slot(design_dir / "evaluation_results", task.problem("01_step"))

    completed = CliRunner().invoke(app, ["recalculate", design_dir.as_posix()])

    assert completed.exit_code == 0, completed.output
    assert len(recording_queue.configs) == 1
    assert json.loads((trial_dir / "verifier" / "reward.json").read_text())["sloc"] > 0


def test_recalculate_design_without_evaluations_is_a_no_op(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """Remeasuring a design without authored evaluations replays nothing."""
    design_dir = tmp_path / "design"
    plan(
        AuthorJob(
            tasks=(Task.from_dir(tasks_root / "pyt"),),
            agent=AgentConfig(name="oracle"),
            n_concurrent=1,
        ),
        design_dir,
    )

    completed = CliRunner().invoke(app, ["recalculate", design_dir.as_posix()])

    assert completed.exit_code == 0, completed.output
    assert recording_queue.configs == ()


def test_recalculate_plans_and_reports_every_cell_an_experiment_lacks(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """An experiment whose cells were never planned reports each one as a rerun."""
    created = invoke_experiment(
        MINIMAL_CONFIG, tasks_root, tmp_path / "runs", "--design-only"
    )
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    shutil.rmtree(experiment / "evaluation_results")
    recording_queue.batches.clear()

    completed = CliRunner().invoke(app, ["recalculate", experiment.as_posix()])

    assert completed.exit_code == 0, completed.output
    assert recording_queue.configs == ()
    document = LdbResult.load(experiment / "ldb-result.json")
    assert {trial.name for trial in document.trials} == {
        cell.trial_name for cell in evaluation_cells(experiment)
    }
    assert len(document.trials) == 4
    assert {trial.outcome for trial in document.trials} == {"rerun"}
    assert document.meta.complete is False


def test_recalculate_preserves_a_fractional_pass_rate(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """The replay scores the recorded counts, so correctness is never replaced."""
    run_dir = tmp_path / "run"
    slot = _graded_slot(run_dir, Task.from_dir(tasks_root / "pyt").problem("01_step"))
    native_path = slot / "result.json"
    native = json.loads(native_path.read_text())
    native["verifier_result"]["rewards"].update(pass_rate=0.625, passed=5, total=8)
    native_path.write_text(json.dumps(native))

    completed = CliRunner().invoke(app, ["recalculate", run_dir.as_posix()])

    assert completed.exit_code == 0, completed.output
    (replayed,) = recording_queue.configs
    assert (
        replayed.verifier.env["LDB_PASSED"],
        replayed.verifier.env["LDB_TOTAL"],
    ) == (
        "5",
        "8",
    )
    reward = json.loads((slot / "verifier" / "reward.json").read_text())
    assert (reward["pass_rate"], reward["passed"], reward["total"]) == (0.625, 5, 8)
    assert reward["reward"] == pytest.approx(0.625**2)
    assert json.loads(native_path.read_text())["verifier_result"]["rewards"] == reward
    trial = json.loads((slot / "trial-report.json").read_text())
    run = json.loads((run_dir / "ldb-result.json").read_text())
    assert run["schema_version"] == 1
    assert run["trials"][0]["pass_rate"] == trial["pass_rate"]
    assert trial["pass_rate"] == 0.625
