"""`ldb run CONFIG` places, plans, launches, and reports one whole experiment."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult
from typer.testing import CliRunner

from lib_design_bench.cli import app
from lib_design_bench.common import TIMESTAMP_FORMAT
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.reports.rebuild import persisted_report
from lib_design_bench.runs.plan import UNSETTLED_DESIGN_REASON
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import Slot
from tests.lib_design_bench.conftest import MINIMAL_CONFIG
from tests.lib_design_bench.conftest import TWO_IMPLEMENTORS_CONFIG
from tests.lib_design_bench.conftest import Outcome
from tests.lib_design_bench.conftest import RecordingQueue
from tests.lib_design_bench.conftest import continue_experiment
from tests.lib_design_bench.conftest import evaluation_cells
from tests.lib_design_bench.conftest import experiment_dir
from tests.lib_design_bench.conftest import flat_output
from tests.lib_design_bench.conftest import invoke_experiment
from tests.lib_design_bench.conftest import seed_slot_artifacts
from tests.lib_design_bench.conftest import seed_slot_failure


def test_run_config_creates_experiment_under_runs_agent_attempts_named_by_start_time(
    tasks_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_queue: RecordingQueue,
) -> None:
    """An unnamed experiment lands directly under the default container, named by its start time."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = CliRunner().invoke(
        app,
        [
            "run",
            str(MINIMAL_CONFIG),
            "--agent",
            "oracle",
            "--model",
            "dummy",
            f"tasks_root={tasks_root}",
        ],
    )

    assert result.exit_code == 0, result.output
    experiment = experiment_dir(workdir / "runs" / "agent_attempts")
    datetime.strptime(experiment.name.removeprefix("run_"), TIMESTAMP_FORMAT)
    assert experiment.name.startswith("run_")
    assert (experiment / "manifest.json").is_file()
    assert (experiment / "ldb-result.json").is_file()


def test_run_name_names_the_experiment_directory(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """`--name` is the experiment directory's exact name, and is never reused."""
    arguments = [
        "run",
        str(MINIMAL_CONFIG),
        "--agent",
        "oracle",
        "--model",
        "dummy",
        "--output",
        str(tmp_path / "experiments"),
        "--name",
        "baseline",
        f"tasks_root={tasks_root}",
    ]

    first = CliRunner().invoke(app, arguments)
    again = CliRunner().invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert (tmp_path / "experiments" / "baseline" / "manifest.json").is_file()
    assert again.exit_code == 2
    assert "already exists" in flat_output(again.output)


def test_resuming_evaluation_child_refreshes_parent_experiment_result(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Resuming the evaluation child replaces a stale experiment result."""
    runs = tmp_path / "runs"
    result = invoke_experiment(MINIMAL_CONFIG, tasks_root, runs)
    assert result.exit_code == 0, result.output
    experiment = experiment_dir(runs)
    evaluation = experiment / "evaluation_results"
    document_path = experiment / "ldb-result.json"
    fresh = LdbResult.load(document_path)
    stale = fresh.model_copy(
        update={
            "trials": (),
            "meta": fresh.meta.model_copy(update={"complete": False}),
        }
    )
    document_path.write_text(stale.to_json())
    recording_queue.batches.clear()

    result = CliRunner().invoke(app, ["resume", str(evaluation)])

    assert result.exit_code == 0, result.output
    assert LdbResult.load(document_path) == fresh
    assert recording_queue.trial_names == ()


def test_continuing_a_finished_experiment_republishes_a_missing_result(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A continuation with no work left still restores the root result."""
    runs = tmp_path / "runs"
    result = invoke_experiment(MINIMAL_CONFIG, tasks_root, runs)
    assert result.exit_code == 0, result.output
    experiment = experiment_dir(runs)
    document_path = experiment / "ldb-result.json"
    fresh = LdbResult.load(document_path)
    document_path.unlink()
    recording_queue.batches.clear()

    continued = continue_experiment(experiment)

    assert continued.exit_code == 0, continued.output
    assert recording_queue.trial_names == ()
    assert LdbResult.load(document_path) == fresh


def test_resuming_design_root_refuses_an_active_evaluation_child_without_mutation(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """The root checks its child lease before it changes either run."""
    runs = tmp_path / "runs"
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, runs)
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(runs)
    child = experiment / "evaluation_results"
    (child / ".resume.lock").write_text("active\n", encoding="utf-8")
    before = {
        path.relative_to(experiment): path.read_bytes()
        for path in experiment.rglob("*")
        if path.is_file()
    }
    recording_queue.batches.clear()

    resumed = CliRunner().invoke(app, ["resume", str(experiment)])

    assert resumed.exit_code != 0
    assert recording_queue.batches == []
    after = {
        path.relative_to(experiment): path.read_bytes()
        for path in experiment.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_resuming_evaluation_child_refuses_an_active_parent_without_mutation(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A child resume leases its parent before rebuilding the parent summary."""
    runs = tmp_path / "runs"
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, runs)
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(runs)
    child = experiment / "evaluation_results"
    (experiment / ".resume.lock").write_text("active\n", encoding="utf-8")
    before = {
        path.relative_to(experiment): path.read_bytes()
        for path in experiment.rglob("*")
        if path.is_file()
    }
    recording_queue.batches.clear()

    resumed = CliRunner().invoke(app, ["resume", str(child)])

    assert resumed.exit_code != 0
    assert recording_queue.batches == []
    after = {
        path.relative_to(experiment): path.read_bytes()
        for path in experiment.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_experiment_uses_one_event_loop_for_design_and_evaluation(
    tasks_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_queue: RecordingQueue,
) -> None:
    """Evaluation Phase Daytona setup can reuse the Design Phase async client."""
    loops: list[asyncio.AbstractEventLoop] = []
    submit_batch = recording_queue.submit_batch

    def record_loop(
        configs: list[TrialConfig],
    ) -> list[Coroutine[Any, Any, TrialResult]]:
        loops.append(asyncio.get_running_loop())
        return submit_batch(configs)

    monkeypatch.setattr(recording_queue, "submit_batch", record_loop)

    result = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")

    assert result.exit_code == 0, result.output
    assert len(loops) == 2
    assert loops[0] is loops[1]


def test_run_config_requires_design_agent_and_model(
    tasks_root: Path, tmp_path: Path
) -> None:
    """The design agent is never taken from the config, so it must be supplied."""
    result = CliRunner().invoke(
        app,
        ["run", str(MINIMAL_CONFIG), f"tasks_root={tasks_root}"],
    )

    assert result.exit_code == 2
    assert "Missing option" in result.output


def test_run_config_submits_every_cell_in_one_launch_batch(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """Every implementor's cells share one Harbor batch, each with its own agent.

    Both implementors name one agent and one model, so a cell's identity can
    only come from its config key.
    """
    result = invoke_experiment(TWO_IMPLEMENTORS_CONFIG, tasks_root, tmp_path / "runs")

    assert result.exit_code == 0, result.output
    design_batch, evaluation_batch = recording_queue.batches
    assert len(design_batch) == 4
    cells = evaluation_cells(experiment_dir(tmp_path / "runs"))
    assert len(cells) == 32
    assert {config.trial_name for config in evaluation_batch} == {
        cell.trial_name for cell in cells
    }
    assert {
        (cell.arm.label, cell.agent.name, cell.agent.model_name) for cell in cells
    } == {("luna", "oracle", "dummy"), ("nova", "oracle", "dummy")}
    assert {
        (cell.arm.label, cell.agent.kwargs["reasoning_effort"]) for cell in cells
    } == {("luna", "high"), ("nova", "low")}


def test_run_config_crosses_every_implementor_with_every_author_attempt(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """One cell exists per implementor, problem, design attempt, and eval attempt."""
    result = invoke_experiment(TWO_IMPLEMENTORS_CONFIG, tasks_root, tmp_path / "runs")

    assert result.exit_code == 0, result.output
    experiment = experiment_dir(tmp_path / "runs")
    document = LdbResult.load(experiment / "ldb-result.json")
    rows = document.trials
    assert {
        (
            row.implementor,
            row.task,
            row.problem,
            document.libraries[row.library].attempt,
            row.attempt,
        )
        for row in rows
    } == {
        (implementor, task, problem, design_attempt, evaluation_attempt)
        for implementor in ("luna", "nova")
        for task, problem in (
            ("pyt", "01_step"),
            ("pyt", "02_step"),
            ("rsj", "01_step"),
            ("rsj", "02_step"),
        )
        for design_attempt in (1, 2)
        for evaluation_attempt in (1, 2)
    }


def test_run_config_writes_complete_experiment_result_and_prints_a_row_per_implementor(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A clean experiment reports every cell finished and shows each implementor.

    The two implementors share an agent and a model, so one row each proves
    the display groups by config key rather than by agent identity.
    """
    result = invoke_experiment(TWO_IMPLEMENTORS_CONFIG, tasks_root, tmp_path / "runs")

    assert result.exit_code == 0, result.output
    experiment = experiment_dir(tmp_path / "runs")
    document = LdbResult.load(experiment / "ldb-result.json")
    assert document.meta.complete is True
    assert document.meta.outcome_counts == {
        "finished": 36,
        "reanalyze": 0,
        "reverify": 0,
        "rerun": 0,
    }
    assert tuple(document.implementors) == ("luna", "nova")
    assert {row.score for row in document.trials} == {1.0}
    assert set(document.implementors) == {"luna", "nova"}
    assert "Implementors" in result.output
    assert "Tasks" in result.output
    assert "luna" in result.output
    assert "nova" in result.output
    assert "32 cells, 4 libraries" in result.output


def test_run_config_marks_result_incomplete_when_evaluation_is_interrupted(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """An interrupted experiment still writes what both phases persisted."""
    recording_queue.outcomes = {
        "pyt__phase-1__author__a1": Outcome(),
        "rsj__phase-1__author__a1": Outcome(),
    }
    recording_queue.default = Outcome(raises=KeyboardInterrupt())

    result = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")

    assert result.exit_code == 130
    experiment = experiment_dir(tmp_path / "runs")
    document = LdbResult.load(experiment / "ldb-result.json")
    assert document.meta.complete is False
    assert len(document.libraries) == 2
    assert {row.outcome for row in document.trials} == {"rerun"}
    assert document.meta.outcome_counts["rerun"] == len(document.trials)
    assert not (experiment / ".resume.lock").exists()
    assert not (experiment / "evaluation_results" / ".resume.lock").exists()
    recording_queue.default = Outcome()
    continued = continue_experiment(experiment)
    assert continued.exit_code == 0, continued.output
    assert LdbResult.load(experiment / "ldb-result.json").meta.complete is True


def _evaluation_slots(experiment: Path) -> tuple[Slot, ...]:
    evaluation = Run.open(experiment / "evaluation_results")
    return tuple(evaluation.slot(cell) for cell in evaluation_cells(experiment))


def test_run_dir_reruns_failed_author_trials_and_then_plans_their_cells(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A task holds back only its own cells until its library exists."""
    failure = TrialResult.model_validate_json(
        (fixtures_root / "outcomes" / "auth_error" / "result.json").read_text(
            encoding="utf-8"
        )
    ).exception_info
    recording_queue.outcomes = {"pyt__phase-1__author__a1": Outcome(exception=failure)}

    failed = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")

    assert failed.exit_code == 1
    experiment = experiment_dir(tmp_path / "runs")
    launched = {config.trial_name for config in recording_queue.batches[-1]}
    assert {
        cell.task.name
        for cell in evaluation_cells(experiment)
        if cell.trial_name in launched
    } == {"rsj"}
    held = {
        trial.name: trial
        for trial in LdbResult.load(experiment / "ldb-result.json").trials
        if trial.task == "pyt"
    }
    assert len(held) == 2
    assert {trial.outcome for trial in held.values()} == {"rerun"}
    assert {trial.incomplete_reason for trial in held.values()} == {
        UNSETTLED_DESIGN_REASON
    }

    recording_queue.outcomes = {}
    recording_queue.batches.clear()
    result = continue_experiment(experiment)

    assert result.exit_code == 0, result.output
    design_batch, evaluation_batch = recording_queue.batches
    assert tuple(config.trial_name for config in design_batch) == (
        "pyt__phase-1__author__a1",
    )
    assert {config.trial_name for config in evaluation_batch} == {
        cell.trial_name
        for cell in evaluation_cells(experiment)
        if cell.task.name == "pyt"
    }
    assert len(evaluation_cells(experiment)) == 4
    assert LdbResult.load(experiment / "ldb-result.json").meta.complete is True


def test_run_dir_can_hold_back_failed_author_trials_instead_of_rerunning(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """`--hold-design-reruns` leaves an errored author trial alone and reports it."""
    failure = TrialResult.model_validate_json(
        (fixtures_root / "outcomes" / "auth_error" / "result.json").read_text(
            encoding="utf-8"
        )
    ).exception_info
    recording_queue.outcomes = {"pyt__phase-1__author__a1": Outcome(exception=failure)}
    failed = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert failed.exit_code == 1
    experiment = experiment_dir(tmp_path / "runs")
    recording_queue.outcomes = {}
    recording_queue.batches.clear()

    result = continue_experiment(experiment, "--hold-design-reruns")

    assert result.exit_code == 1
    launched = {
        config.trial_name for batch in recording_queue.batches for config in batch
    }
    assert "pyt__phase-1__author__a1" not in launched
    assert not any(
        cell.trial_name in launched
        for cell in evaluation_cells(experiment)
        if cell.task.name == "pyt"
    )
    document = LdbResult.load(experiment / "ldb-result.json")
    assert len(document.trials) == len(evaluation_cells(experiment)) == 4
    assert document.meta.complete is False


def test_new_experiment_rejects_hold_design_reruns(
    tasks_root: Path, tmp_path: Path
) -> None:
    """The flag describes continuation; a new experiment has nothing to hold."""
    result = invoke_experiment(
        MINIMAL_CONFIG, tasks_root, tmp_path / "runs", "--hold-design-reruns"
    )

    assert result.exit_code != 0
    assert "hold-design-reruns" in result.output


def test_run_dir_reruns_and_reverifies_evaluation_cells_then_rebuilds_result(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_queue: RecordingQueue,
) -> None:
    """A broken cell is relaunched, an ungraded one is regraded, both are reported."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    broken, ungraded, *_ = _evaluation_slots(experiment)
    seed_slot_failure(broken, fixtures_root, "provider_error")
    seed_slot_failure(ungraded, fixtures_root, "verifier_error")
    seed_slot_artifacts(ungraded)
    recording_queue.batches.clear()
    loops: list[asyncio.AbstractEventLoop] = []
    submit_batch = recording_queue.submit_batch

    def record_loop(
        configs: list[TrialConfig],
    ) -> list[Coroutine[Any, Any, TrialResult]]:
        loops.append(asyncio.get_running_loop())
        return submit_batch(configs)

    monkeypatch.setattr(recording_queue, "submit_batch", record_loop)

    result = continue_experiment(experiment)

    assert result.exit_code == 0, result.output
    assert set(recording_queue.trial_names) == {broken.dir.name, ungraded.dir.name}
    assert len(loops) == 2
    assert loops[0] is loops[1]
    relaunched, regraded = broken.result(), ungraded.result()
    assert relaunched is not None and relaunched.exception_info is None
    assert regraded is not None and regraded.exception_info is None
    assert Run.open(experiment / "evaluation_results").manifest.verification is not None
    document = LdbResult.load(experiment / "ldb-result.json")
    assert document.meta.complete is True
    assert document.meta.outcome_counts == {
        "finished": 6,
        "reanalyze": 0,
        "reverify": 0,
        "rerun": 0,
    }


def test_run_dir_reports_finished_cells_when_verifier_replay_cannot_start(
    tasks_root: Path,
    fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_queue: RecordingQueue,
) -> None:
    """A replay that fails before grading leaves its slot re-verify and still reports."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    ungraded, *_ = _evaluation_slots(experiment)
    seed_slot_failure(ungraded, fixtures_root, "verifier_error")
    seed_slot_artifacts(ungraded)
    before = ungraded.result()

    def missing_source(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("metrics/standalone/fingerprint.py")

    monkeypatch.setattr(
        "lib_design_bench.pipeline.replay.replay_and_update",
        missing_source,
    )

    result = continue_experiment(experiment)

    assert result.exit_code == 0, result.output
    assert ungraded.result() == before
    document = LdbResult.load(experiment / "ldb-result.json")
    assert document.meta.complete is False
    assert document.meta.outcome_counts == {
        "finished": 5,
        "reanalyze": 0,
        "reverify": 1,
        "rerun": 0,
    }


@pytest.mark.parametrize(
    "option",
    [
        ["--output", "elsewhere"],
        ["--agent", "oracle"],
        ["--model", "dummy"],
        ["--reasoning", "high"],
        ["--agent-version", "1.2.3"],
        ["--agent-kwargs", "temperature=0.2"],
        ["--agent-env", "FOO=bar"],
        ["--name", "baseline"],
    ],
)
def test_run_dir_rejects_request_shaping_options(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
    option: list[str],
) -> None:
    """A continued experiment measures the setup it was published with."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")

    result = continue_experiment(experiment, *option)

    assert result.exit_code == 2
    assert "shape a new experiment" in flat_output(result.output)


def test_run_config_applies_overrides_and_records_them(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A `KEY=VALUE` override changes what runs and is published with the run."""
    override = "evaluation.agents.impl.kwargs.reasoning_effort=low"
    result = invoke_experiment(
        MINIMAL_CONFIG, tasks_root, tmp_path / "runs", override, "evaluation.attempts=2"
    )

    assert result.exit_code == 0, result.output
    record = Run.open(experiment_dir(tmp_path / "runs")).manifest.experiment
    assert record is not None
    assert record.overrides[-2:] == (override, "evaluation.attempts=2")
    assert record.config.evaluation.attempts == 2
    assert record.config.evaluation.agents["impl"].kwargs["reasoning_effort"] == "low"
    assert {
        cell.attempt for cell in evaluation_cells(experiment_dir(tmp_path / "runs"))
    } == {1, 2}


def test_run_config_gives_every_agent_the_cli_env_and_hosts(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """`--agent-env` and `--allow-agent-host` reach the design agent and every cell."""
    result = invoke_experiment(
        MINIMAL_CONFIG,
        tasks_root,
        tmp_path / "runs",
        "--agent-env",
        "FOO=bar",
        "--allow-agent-host",
        "example.com",
    )

    assert result.exit_code == 0, result.output
    experiment = experiment_dir(tmp_path / "runs")
    record = Run.open(experiment).manifest.experiment
    assert record is not None
    cells = evaluation_cells(experiment)
    assert cells
    for agent in (record.design_agent, *(cell.agent for cell in cells)):
        assert agent.env["FOO"] == "bar"
        assert "example.com" in agent.extra_allowed_hosts


def test_run_dir_rejects_overrides_outside_environment(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A continued experiment only moves where it runs, never what it measures."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output

    result = continue_experiment(
        experiment_dir(tmp_path / "runs"), "evaluation.attempts=2"
    )

    assert result.exit_code == 2
    assert "only accepts overrides under environment" in flat_output(result.output)


@pytest.mark.parametrize(
    "option",
    [
        ["-n", "2"],
        ["environment.kwargs.auto_snapshot=true"],
        ["--json"],
        ["--debug"],
    ],
)
def test_run_dir_allows_execution_options(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
    option: list[str],
) -> None:
    """Where and how fast an experiment runs stays open after it was published."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output

    result = continue_experiment(experiment_dir(tmp_path / "runs"), *option)

    assert result.exit_code == 0, result.output


def test_run_dir_continues_after_its_task_sources_changed(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A changed checked-in task is a warning; the manifest still decides."""
    tasks = tmp_path / "tasks"
    shutil.copytree(tasks_root, tasks)
    created = invoke_experiment(MINIMAL_CONFIG, tasks, tmp_path / "runs")
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    (tasks / "pyt" / "design" / "instruction.md").write_text(
        "a different authoring instruction\n", encoding="utf-8"
    )
    broken, *_ = _evaluation_slots(experiment)
    broken.clear()
    recording_queue.batches.clear()

    result = continue_experiment(experiment)

    assert result.exit_code == 0, result.output
    assert recording_queue.trial_names == (broken.dir.name,)
    assert LdbResult.load(experiment / "ldb-result.json").meta.complete is True


def test_run_dir_refuses_active_lease_without_force(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """Two writers on one experiment directory would corrupt its evidence."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    (experiment / ".resume.lock").write_text("pid=1\n", encoding="utf-8")

    refused = continue_experiment(experiment)

    assert refused.exit_code == 1
    assert "lease already exists" in str(refused.exception)
    assert continue_experiment(experiment, "--force").exit_code == 0


@pytest.mark.parametrize("continued", (False, True), ids=("config", "experiment-dir"))
def test_design_only_stops_after_design_run_with_partial_result(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
    continued: bool,
) -> None:
    """Both forms author libraries, plan but run no cell, and report every cell."""
    result = invoke_experiment(
        MINIMAL_CONFIG,
        tasks_root,
        tmp_path / "runs",
        "--design-only",
    )
    assert result.exit_code == 0, result.output
    experiment = experiment_dir(tmp_path / "runs")
    if continued:
        recording_queue.batches.clear()
        result = continue_experiment(experiment, "--design-only")
        assert result.exit_code == 0, result.output
        assert recording_queue.batches == []

    document = LdbResult.load(experiment / "ldb-result.json")
    assert document.meta.type == "experiment"
    assert document.meta.complete is False
    assert {trial.name for trial in document.trials} == {
        cell.trial_name for cell in evaluation_cells(experiment)
    }
    assert len(document.trials) == 4
    assert {trial.outcome for trial in document.trials} == {"rerun"}
    assert {library.task for library in document.libraries.values()} == {"pyt", "rsj"}
    assert "Design run" in result.output


def test_continued_design_only_applies_provider_override_to_design_reruns(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A continuation override reaches unfinished author trials but not the Evaluation Phase."""
    created = invoke_experiment(
        MINIMAL_CONFIG,
        tasks_root,
        tmp_path / "runs",
        "environment.type=modal",
        "--design-only",
    )
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    design = Run.open(experiment)
    launch, *_ = design.launches()
    design.slot(launch).clear()
    recording_queue.batches.clear()

    continued = continue_experiment(
        experiment,
        "--design-only",
        "environment.type=docker",
        "environment.kwargs.custom_flag=true",
    )

    assert continued.exit_code == 0, continued.output
    assert len(recording_queue.configs) == 1
    (config,) = recording_queue.configs
    assert config.environment.type == "docker"
    assert config.environment.kwargs["custom_flag"] is True
    evaluation = Run.open(experiment / "evaluation_results").request()
    assert evaluation.environment.type == EnvironmentType.MODAL
    assert {
        trial.outcome for trial in LdbResult.load(experiment / "ldb-result.json").trials
    } == {"rerun"}


def test_continued_design_reports_result_environment_without_relabeling_retained_attempt(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A rerun report uses Docker while an unselected Modal result stays Modal."""
    created = invoke_experiment(
        TWO_IMPLEMENTORS_CONFIG,
        tasks_root,
        tmp_path / "runs",
        "environment.type=modal",
        "--design-only",
    )
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    design = Run.open(experiment)
    rerun = next(launch for launch in design.launches() if launch.task.name == "pyt")
    retained = next(launch for launch in design.launches() if launch.task.name == "rsj")
    design.slot(rerun).clear()
    recording_queue.batches.clear()

    continued = continue_experiment(
        experiment,
        "--task",
        "pyt",
        "--design-only",
        "environment.type=docker",
    )

    assert continued.exit_code == 0, continued.output
    persisted = Run.open(experiment)
    persisted_launches = {launch.trial_name: launch for launch in persisted.launches()}
    assert (
        persisted_launches[rerun.trial_name].environment.type == EnvironmentType.MODAL
    )
    assert (
        persisted_launches[retained.trial_name].environment.type
        == EnvironmentType.MODAL
    )
    rerun_result = persisted.slot(rerun).result()
    retained_result = persisted.slot(retained).result()
    assert rerun_result is not None
    assert retained_result is not None
    assert rerun_result.config.environment.type == EnvironmentType.DOCKER
    assert retained_result.config.environment.type == EnvironmentType.MODAL

    rerun_trial = persisted.slot(rerun).trial_report()
    retained_trial = persisted.slot(retained).trial_report()
    report = persisted_report(persisted)
    assert rerun_trial is not None
    assert retained_trial is not None
    assert rerun_trial.environment_type == "docker"
    assert retained_trial.environment_type == "modal"
    by_name = {attempt.trial_name: attempt for attempt in report.attempts}
    assert by_name[rerun.trial_name].environment_type == "docker"
    assert by_name[retained.trial_name].environment_type == "modal"


def test_run_dir_after_design_only_plans_and_runs_every_cell(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """Dropping the flag makes the deferred evaluation an ordinary first run."""
    created = invoke_experiment(
        MINIMAL_CONFIG,
        tasks_root,
        tmp_path / "runs",
        "--design-only",
    )
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    recording_queue.batches.clear()

    result = continue_experiment(experiment)

    assert result.exit_code == 0, result.output
    (evaluation_batch,) = recording_queue.batches
    assert {config.trial_name for config in evaluation_batch} == {
        cell.trial_name for cell in evaluation_cells(experiment)
    }
    assert len(evaluation_batch) == 4
    document = LdbResult.load(experiment / "ldb-result.json")
    assert document.meta.type == "experiment"
    assert document.meta.complete is True


def test_json_prints_experiment_result_document(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """`--json` prints exactly the document the experiment persisted."""
    result = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs", "--json")

    assert result.exit_code == 0, result.output
    experiment = experiment_dir(tmp_path / "runs")
    assert json.loads(result.stdout) == json.loads(
        (experiment / "ldb-result.json").read_text(encoding="utf-8")
    )


def test_continue_switches_evaluation_provider_and_preserves_it_when_omitted(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A saved Docker evaluation honors Modal and keeps it on the next resume."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output
    experiment = experiment_dir(tmp_path / "runs")
    for options in (("environment.type=modal",), ()):
        for slot in _evaluation_slots(experiment):
            slot.clear()
        recording_queue.batches.clear()
        continued = continue_experiment(experiment, *options)
        assert continued.exit_code == 0, continued.output
        assert len(recording_queue.configs) == 4
        assert {config.environment.type for config in recording_queue.configs} == {
            "modal"
        }
        assert all(
            config.environment.kwargs["modal_vm_runtime"] is True
            for config in recording_queue.configs
        )
        saved = Run.open(experiment / "evaluation_results").request()
        assert saved.environment.type == "modal"
