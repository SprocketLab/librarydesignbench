"""`ldb verify` and `ldb static` run the oracle over checked-in references."""

from __future__ import annotations

import json
import shutil
from datetime import UTC
from datetime import datetime
from pathlib import Path

from harbor.models.trial.config import AgentConfig
from typer.testing import CliRunner

from lib_design_bench.cli import app
from lib_design_bench.cli.verify import verification_arms
from lib_design_bench.models.job import Job
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.task import Task
from lib_design_bench.runs.store import expand
from tests.lib_design_bench.conftest import UNREFERENCED_METRICS
from tests.lib_design_bench.conftest import RecordingQueue

RUN_TIMESTAMP = datetime(2026, 9, 4, 17, 12, 22, tzinfo=UTC)


def test_verification_arms_only_expand_checked_in_solution_pairs(
    tasks_root: Path, tmp_path: Path
) -> None:
    """Verifier reference coverage scopes only oracle verification, not evaluation."""
    task_dir = tmp_path / "pyt"
    shutil.copytree(tasks_root / "pyt", task_dir)
    shutil.rmtree(task_dir / "evaluation" / "02_step" / "solution")
    task = Task.from_dir(task_dir)
    problems = task.active_problems()
    request = Job(
        problems=problems,
        arms=verification_arms(task, problems, AgentConfig(name="oracle")),
        n_concurrent=1,
    )

    launches = expand(request, run_timestamp=RUN_TIMESTAMP)

    evaluation_launches = tuple(
        launch for launch in launches if isinstance(launch, EvaluationLaunch)
    )
    assert tuple(
        (launch.library_name, launch.problem.name) for launch in evaluation_launches
    ) == (
        ("no-library", "01_step"),
        ("more-itertools", "01_step"),
    )


def test_static_reference_refresh_measures_each_reference_arm_with_tests_skipped(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """Every reference is rewritten from its own oracle trial's measurement."""
    tasks = tmp_path / "tasks"
    shutil.copytree(tasks_root, tasks)
    references = sorted(tasks.rglob("static_reference.json"))
    original = {path: json.loads(path.read_text()) for path in references}
    for path in references:
        stale = json.loads(path.read_text())
        stale["metrics"]["sloc"] += 100
        path.write_text(json.dumps(stale))

    completed = CliRunner().invoke(app, ["static", tasks.as_posix()])

    assert completed.exit_code == 0, completed.output
    assert all(
        config.verifier.env == {"LDB_SKIP_TESTS": "1"}
        for config in recording_queue.configs
    )
    assert len(recording_queue.configs) >= len(references)
    for path in references:
        refreshed = json.loads(path.read_text())
        assert refreshed["library"] == original[path]["library"]
        assert refreshed["metrics"]["sloc"] == original[path]["metrics"]["sloc"] + 100


def test_static_reference_refresh_bootstraps_a_missing_reference(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """A problem without a reference is measured on its first solved library."""
    tasks = tmp_path / "tasks"
    shutil.copytree(tasks_root, tasks)
    reference = (
        tasks / "pyt" / "evaluation" / "01_step" / "tests" / "static_reference.json"
    )
    original = json.loads(reference.read_text())
    reference.unlink()

    completed = CliRunner().invoke(app, ["static", tasks.as_posix()])

    assert completed.exit_code == 0, completed.output
    refreshed = json.loads(reference.read_text())
    assert refreshed["library"] == original["library"]
    assert refreshed["metrics"]["sloc"] == UNREFERENCED_METRICS["sloc"]
    assert all(
        config.verifier.env == {"LDB_SKIP_TESTS": "1"}
        for config in recording_queue.configs
    )


def test_static_reference_refresh_names_a_problem_it_cannot_bootstrap(
    tmp_path: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """Without a reference or a library solution, nothing runs and the problem is named."""
    tasks = tmp_path / "tasks"
    shutil.copytree(tasks_root, tasks)
    problem = tasks / "pyt" / "evaluation" / "01_step"
    (problem / "tests" / "static_reference.json").unlink()
    shutil.rmtree(problem / "solution" / "more-itertools")

    completed = CliRunner().invoke(app, ["static", tasks.as_posix()])

    assert completed.exit_code == 2
    assert "pyt/01_step" in completed.output
    assert recording_queue.configs == ()
