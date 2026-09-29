"""Public command grammar and Design/Evaluation routing behavior."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult
from harbor.models.trial.config import EnvironmentConfig
from typer.testing import CliRunner

from lib_design_bench.cli import app
from lib_design_bench.cli.common import oracle_agent
from lib_design_bench.cli.eval import DEFAULT_PROMPT
from lib_design_bench.cli.eval import selected_existing_library_entries
from lib_design_bench.common import TIMESTAMP_FORMAT
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.models.task import Task
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import Run
from tests.lib_design_bench.conftest import RecordingQueue
from tests.lib_design_bench.conftest import seed_finished_slot

_ORACLE = ("--agent", "oracle", "--model", "dummy")


@pytest.mark.parametrize(
    ("selected_libraries", "expected"),
    (
        (("pytest",), ("pytest",)),
        (("more-itertools", "pytest"), ("more-itertools", "pytest")),
    ),
    ids=("secondary", "several"),
)
def test_existing_library_selection_defaults_to_spine_only(
    tasks_root: Path,
    selected_libraries: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    """An omitted selection cannot expand secondary libraries; an explicit one can."""
    task = Task.from_dir(tasks_root / "pyt")

    selected = selected_existing_library_entries(task, selected_libraries)

    assert tuple(name for name, _ in selected) == expected


def test_existing_library_explicit_selection_skips_undeclared_tasks(
    tasks_root: Path,
) -> None:
    """A selected comparator cannot create an undeclared comparison arm."""
    pyt = Task.from_dir(tasks_root / "pyt")
    rsj = Task.from_dir(tasks_root / "rsj")

    pyt_selected = selected_existing_library_entries(pyt, ("pytest",))
    rsj_selected = selected_existing_library_entries(rsj, ("pytest",))

    assert tuple(name for name, _ in pyt_selected) == ("pytest",)
    assert rsj_selected == ()


def test_existing_library_rejects_unknown_explicit_library(
    tasks_root: Path,
) -> None:
    """The CLI rejects an explicit library no selected task declares."""
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "existing-library",
            *_ORACLE,
            f"tasks_root={tasks_root}",
            "--task",
            "pyt",
            "--existing-library",
            "unknown",
        ],
    )

    assert result.exit_code == 2
    assert "do not declare existing library/libraries:" in result.output
    assert "unknown" in result.output


def _design_source(tasks_root: Path, tmp_path: Path, fixtures_root: Path) -> Path:
    """Plan a Design Run that finished without capturing a library.

    Authored evaluation refuses a source whose author slots could still be
    rerun, and a finished slot holding no artifact needs no Harbor launch.
    """
    source = tmp_path / "design"
    plan(
        AuthorJob(
            tasks=(Task.from_dir(tasks_root / "pyt"),),
            agent=oracle_agent(),
            prompt_template="{{ instruction }}",
            n_concurrent=1,
        ),
        source,
    )
    _finish_author_slots(source, fixtures_root)
    return source


def _finish_author_slots(source: Path, fixtures_root: Path) -> None:
    """Give every planned author slot of a Design Run a finished result."""
    run = Run.open(source)
    for launch in run.launches():
        seed_finished_slot(run.slot(launch), fixtures_root)


def test_authored_evaluation_uses_alternate_root_task_by_name(
    tasks_root: Path, tmp_path: Path, fixtures_root: Path
) -> None:
    """Authored conditions retain source artifacts when problems use another root."""
    source = _design_source(tasks_root, tmp_path, fixtures_root)
    alternate_root = tmp_path / "alternate-tasks"
    shutil.copytree(tasks_root / "pyt", alternate_root / "pyt")

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            str(source),
            *_ORACLE,
            f"tasks_root={alternate_root}",
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    request = Run.open(_only_run(tmp_path / "evaluations")).request()
    assert isinstance(request, Job)
    assert request.problems[0].task.source_dir == alternate_root / "pyt"
    assert isinstance(request.arms[0].condition, AuthoredArtifact)
    assert request.arms[0].condition.task.source_dir == alternate_root / "pyt"


def test_authored_evaluation_defaults_to_docker_over_design_run_environment(
    tasks_root: Path, tmp_path: Path, fixtures_root: Path
) -> None:
    """The command's environment decides where cells run, not the Design Run's."""
    source = tmp_path / "design"
    plan(
        AuthorJob(
            tasks=(Task.from_dir(tasks_root / "pyt"),),
            agent=oracle_agent(),
            prompt_template="{{ instruction }}",
            n_concurrent=1,
            environment=EnvironmentConfig(
                type=EnvironmentType.DAYTONA,
                kwargs={"auto_snapshot": False, "region": "us"},
            ),
        ),
        source,
    )
    _finish_author_slots(source, fixtures_root)

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            str(source),
            *_ORACLE,
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    request = Run.open(_only_run(tmp_path / "evaluations")).request()
    assert isinstance(request, Job)
    assert request.environment.type is EnvironmentType.DOCKER
    assert request.environment.kwargs == {}


def test_authored_evaluation_persists_overridden_environment_and_sandbox(
    tasks_root: Path, tmp_path: Path, fixtures_root: Path
) -> None:
    """`environment.*` overrides and sandbox flags reach the request."""
    source = _design_source(tasks_root, tmp_path, fixtures_root)

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            str(source),
            *_ORACLE,
            "environment.type=modal",
            'environment.kwargs.app_name="ldb-tests"',
            'environment.kwargs.secrets=["registry"]',
            "--cpus",
            "3",
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    request = Run.open(_only_run(tmp_path / "evaluations")).request()
    assert isinstance(request, Job)
    assert request.environment.type is EnvironmentType.MODAL
    assert request.environment.kwargs == {
        "app_name": "ldb-tests",
        "secrets": ["registry"],
        "modal_vm_runtime": True,
        "sandbox_timeout_secs": 21_600,
    }
    assert (
        request.environment.override_cpus,
        request.environment.override_memory_mb,
        request.environment.override_storage_mb,
    ) == (3, 4096, 10240)


def test_standalone_control_submits_modal_environment(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A runnable CLI control submits Modal configs without Docker setup."""
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "no-library",
            *_ORACLE,
            f"tasks_root={tasks_root}",
            "environment.type=modal",
            "--task",
            "pyt",
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    configs = recording_queue.configs
    assert configs
    assert all(config.environment.type is EnvironmentType.MODAL for config in configs)
    assert all(
        config.environment.import_path
        == "lib_design_bench.harbor.environments:LdbModalEnvironment"
        for config in configs
    )
    assert all(config.environment.mounts is None for config in configs)
    (run_dir,) = (tmp_path / "evaluations").iterdir()
    assert Run.open(run_dir).request().environment.import_path is None


def test_existing_library_selects_qualified_pairs(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """Existing-library evaluation plans one default-spine cell per qualified pair."""
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "existing-library",
            *_ORACLE,
            f"tasks_root={tasks_root}",
            "--problem",
            "pyt/01_step",
            "--problem",
            "rsj/02_step",
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    request = Run.open(_only_run(tmp_path / "evaluations")).request()
    assert isinstance(request, Job)
    selected = []
    for arm in request.arms:
        assert isinstance(arm.condition, ExistingLibrary)
        selected.append((arm.condition.task.name, arm.condition.name))
    assert selected == [("pyt", "more-itertools"), ("rsj", "itertools")]
    assert tuple((problem.task.name, problem.name) for problem in request.problems) == (
        ("pyt", "01_step"),
        ("rsj", "02_step"),
    )
    assert len(recording_queue.configs) == 2


def test_eval_runs_the_command_line_agent_with_official_defaults(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """The one arm is the `--agent`/`--model` implementor under official settings."""
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "no-library",
            f"tasks_root={tasks_root}",
            *_ORACLE,
            "--reasoning",
            "high",
            "--task",
            "pyt",
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    run_dir = _only_run(tmp_path / "evaluations")
    assert LdbResult.model_validate(json.loads(result.stdout)) == LdbResult.load(
        run_dir / "ldb-result.json"
    )
    request = Run.open(run_dir).request()
    assert isinstance(request, Job)
    assert {arm.label for arm in request.arms} == {"oracle__dummy"}
    assert {arm.agent.kwargs["reasoning_effort"] for arm in request.arms} == {"high"}
    assert request.attempts == (1,)
    assert request.prompt_template == DEFAULT_PROMPT.read_text(encoding="utf-8")
    assert (
        request.environment.override_cpus,
        request.environment.override_memory_mb,
        request.environment.override_storage_mb,
    ) == (2, 4096, 10240)
    assert run_dir.name.startswith("evaluation_")
    datetime.strptime(run_dir.name.removeprefix("evaluation_"), TIMESTAMP_FORMAT)
    job_config = JobConfig.model_validate_json((run_dir / "config.json").read_text())
    assert (job_config.job_name, job_config.jobs_dir) == (run_dir.name, run_dir.parent)
    assert job_config.n_concurrent_trials == request.n_concurrent
    assert [agent.name for agent in job_config.agents] == ["oracle"]
    job_result = JobResult.model_validate_json((run_dir / "result.json").read_text())
    assert job_result.n_total_trials == len(Run.open(run_dir).launches())
    assert job_result.finished_at is not None
    assert not tuple(run_dir.glob(".harbor-*"))


def test_standalone_evaluation_name_names_the_run_directory(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """`--name` is the run directory's exact name below `--output`."""
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "no-library",
            f"tasks_root={tasks_root}",
            *_ORACLE,
            "--task",
            "pyt",
            "--output",
            str(tmp_path / "evaluations"),
            "--name",
            "floor",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _only_run(tmp_path / "evaluations") == tmp_path / "evaluations" / "floor"


def test_standalone_evaluation_never_reuses_a_start_time_directory(
    tasks_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_queue: RecordingQueue,
) -> None:
    """Two unnamed evaluations started in the same second do not share a run."""
    monkeypatch.setattr(
        "lib_design_bench.cli.common.now_timestamp",
        lambda: "2026-09-29T00-00-00Z",
    )
    arguments = [
        "eval",
        "no-library",
        f"tasks_root={tasks_root}",
        *_ORACLE,
        "--task",
        "pyt",
        "--output",
        str(tmp_path / "evaluations"),
    ]

    first = CliRunner().invoke(app, arguments)
    again = CliRunner().invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert again.exit_code == 2
    assert "already exists" in _flat(again.output)


def test_authored_evaluation_selects_qualified_pairs_across_tasks(
    tasks_root: Path,
    tmp_path: Path,
    fixtures_root: Path,
    recording_queue: RecordingQueue,
) -> None:
    """Authored evaluation carries qualified pairs into its persisted request."""
    source = tmp_path / "design"
    plan(
        AuthorJob(
            tasks=(
                Task.from_dir(tasks_root / "pyt"),
                Task.from_dir(tasks_root / "rsj"),
            ),
            agent=oracle_agent(),
            prompt_template="{{ instruction }}",
            n_concurrent=1,
        ),
        source,
    )
    _finish_author_slots(source, fixtures_root)

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            str(source),
            *_ORACLE,
            "--problem",
            "pyt/02_step",
            "--problem",
            "rsj/01_step",
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    request = Run.open(_only_run(tmp_path / "evaluations")).request()
    assert isinstance(request, Job)
    assert tuple((problem.task.name, problem.name) for problem in request.problems) == (
        ("pyt", "02_step"),
        ("rsj", "01_step"),
    )
    assert recording_queue.configs == ()


def test_authored_evaluation_rejects_raw_provider_credentials(
    tasks_root: Path, tmp_path: Path, fixtures_root: Path
) -> None:
    """Provider credentials are never accepted or echoed in a persisted request."""
    source = _design_source(tasks_root, tmp_path, fixtures_root)

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            str(source),
            *_ORACLE,
            "environment.kwargs.api_key=private-key",
        ],
    )

    assert result.exit_code == 2
    assert "native environment variables" in _flat(result.output)
    assert "private-key" not in result.output


def test_authored_evaluation_rejects_typed_output_container(
    tasks_root: Path, tmp_path: Path, fixtures_root: Path
) -> None:
    """A typed output directory must be continued through resume instead."""
    source = _design_source(tasks_root, tmp_path, fixtures_root)

    result = CliRunner().invoke(
        app,
        ["eval", "design", str(source), *_ORACLE, "--output", str(source)],
    )

    assert result.exit_code == 2
    assert "Use `ldb resume`" in _flat(result.output)


def test_authored_evaluation_reads_a_source_running_its_own_evaluation(
    tasks_root: Path, tmp_path: Path, fixtures_root: Path
) -> None:
    """A live lease on the source cannot withhold its finished author artifacts.

    A Design Run holds its lease while its own Evaluation Phase runs, and that phase
    rewrites no author slot, so its captured libraries stay readable.
    """
    source = _design_source(tasks_root, tmp_path, fixtures_root)
    (source / ".resume.lock").write_text("pid=1\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            str(source),
            *_ORACLE,
            "--output",
            str(tmp_path / "evaluations"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _only_run(tmp_path / "evaluations").is_dir()


def test_authored_evaluation_accepts_a_relative_source_path(
    tasks_root: Path,
    tmp_path: Path,
    fixtures_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source named relative to the working directory is resolved at the edge.

    Authored conditions record absolute artifact paths, so the command must
    resolve the path an operator types rather than pass it through.
    """
    _design_source(tasks_root, tmp_path, fixtures_root)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            "design",
            *_ORACLE,
            "--output",
            "evaluations",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _only_run(tmp_path / "evaluations").is_dir()


def test_authored_evaluation_rejects_an_unfinished_author_slot(
    tasks_root: Path, tmp_path: Path
) -> None:
    """An author slot a continued design run would rerun cannot be evaluated.

    Continuing the source clears such a slot and authors it again, so an
    evaluation reading it would mount an artifact that disappears mid-run.
    """
    source = tmp_path / "design"
    plan(
        AuthorJob(
            tasks=(Task.from_dir(tasks_root / "pyt"),),
            agent=oracle_agent(),
            prompt_template="{{ instruction }}",
            n_concurrent=1,
        ),
        source,
    )

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "design",
            str(source),
            *_ORACLE,
            "--output",
            str(tmp_path / "evaluations"),
        ],
    )

    assert result.exit_code == 2
    assert "author attempts all settled" in _flat(result.output)
    assert not (tmp_path / "evaluations").exists()


def test_authored_evaluation_defaults_output_container_to_runs(
    tasks_root: Path,
    tmp_path: Path,
    fixtures_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone evaluation writes below `runs/`, never inside its Design Run."""
    source = _design_source(tasks_root, tmp_path, fixtures_root)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = CliRunner().invoke(
        app, ["eval", "design", str(source), *_ORACLE, "--json"]
    )

    assert result.exit_code == 0, result.output
    run_name = _only_run(workdir / "runs").name
    assert run_name.startswith("evaluation_")
    datetime.strptime(run_name.removeprefix("evaluation_"), TIMESTAMP_FORMAT)
    assert not (source / "evaluation_results").exists()


def _only_run(output: Path) -> Path:
    """Return the one run directory an evaluation wrote below its output root."""
    (run_dir,) = tuple(output.iterdir())
    return run_dir


def _flat(output: str) -> str:
    """Join one boxed CLI message into a single line, so it reads as written."""
    return " ".join(output.replace("\u2502", " ").split())


def test_verify_run_keeps_harbor_override_apart_from_task_filter(
    tmp_path: Path,
) -> None:
    """`--harbor-task` stays reachable next to the `--task` Task filter."""
    command = ["verify", "run", str(tmp_path), "--update", "--task", "pyt"]

    result = CliRunner().invoke(app, [*command, "--harbor-task", str(tmp_path)])

    assert result.exit_code == 2
    assert "cannot be combined with `--harbor-task`" in result.output
