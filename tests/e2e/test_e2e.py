"""Exact artifact contracts for the typed command pipeline."""

# ruff: noqa: D103

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lib_design_bench.cli import app
from lib_design_bench.models import RunReport
from lib_design_bench.models import StaticMetrics
from lib_design_bench.models import UsageReport
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.reports.rebuild import persisted_report
from lib_design_bench.runs.store import EVALUATION_RESULTS_DIR_NAME
from lib_design_bench.runs.store import Run

pytestmark = [pytest.mark.docker, pytest.mark.slow_e2e]
_ROOT = Path(__file__).parents[1] / "fixtures" / "tasks"
_REJECTING = Path(__file__).parent / "fixtures" / "rejecting_verifier.sh"
_RUNNER = CliRunner()
_EXPERIMENT_CONFIG = (
    Path(__file__).parents[1] / "fixtures" / "experiments" / "minimal.yaml"
)
_ORACLE = ("--agent", "oracle", "--model", "dummy")
_PY = frozenset(("README.md", "main.py", "run.sh"))
_RS = frozenset(("CUTOVER_MODE", "Cargo.toml", "README.md", "run.sh", "src/main.rs"))


@dataclass(frozen=True)
class Expected:
    metrics: tuple[int, int, int, int, float, int]
    simplicity: float
    score: float
    files: frozenset[str]


_FLOOR = {
    ("pyt", "01_step"): Expected(
        (31, 34, 8, 11, 430.33, 201),
        0.12208608388587741,
        0.12208608388587741,
        _PY,
    ),
    ("pyt", "02_step"): Expected(
        (17, 20, 5, 7, 211.52, 121),
        0.19926619569915713,
        0.19926619569915713,
        _PY,
    ),
    ("rsj", "01_step"): Expected((3, 4, 0, 1, 10.0, 22), 2.226, 1.0, _RS),
    ("rsj", "02_step"): Expected((3, 4, 0, 1, 10.0, 22), 2.0425, 1.0, _RS),
}
_CEILING = {
    ("pyt", "01_step"): Expected(
        (4, 4, 1, 1, 66.61, 45), 1.0, 1.0, _PY | {"setup-hook-ran"}
    ),
    ("pyt", "02_step"): Expected(
        (4, 4, 0, 0, 53.77, 43), 1.0, 1.0, _PY | {"setup-hook-ran"}
    ),
}


def _run(args: list[str]) -> None:
    result = _RUNNER.invoke(app, args)
    assert result.exit_code == 0, result.output


def _only(root: Path) -> Run:
    manifests = tuple(
        path
        for path in root.rglob("manifest.json")
        if (path.parent / "ldb-config.json").is_file()
    )
    assert len(manifests) == 1
    return Run.open(manifests[0].parent)


def _report(run: Run) -> RunReport:
    report = persisted_report(run)
    return report


def _metrics(value: StaticMetrics | None) -> tuple[int, int, int, int, float, int]:
    assert value is not None
    assert isinstance(value.stmts, int)
    assert isinstance(value.sloc, int)
    assert isinstance(value.cog_complex, int)
    assert isinstance(value.cyc_complex, int)
    assert isinstance(value.halstead_volume, int | float)
    assert isinstance(value.parse_tokens, int)
    return (
        value.stmts,
        value.sloc,
        value.cog_complex,
        value.cyc_complex,
        float(value.halstead_volume),
        value.parse_tokens,
    )


def _files(run: Run, trial: str) -> frozenset[str]:
    workspace = run.dir / trial / "artifacts" / "workspace"
    return frozenset(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
    )


def _assert_measurement(
    run: Run, side: str, expected: dict[tuple[str, str], Expected]
) -> None:
    report = _report(run)
    assert len(report.attempts) == len(expected)
    assert {attempt.library_kind for attempt in report.attempts} == {side}
    attempts = {(a.task, a.problem): a for a in report.attempts}
    assert {
        key: (
            a.pass_rate,
            _metrics(a.static_analysis),
            a.simplicity,
            a.score,
            _files(run, a.trial_name),
        )
        for key, a in attempts.items()
    } == {
        key: (1.0, value.metrics, value.simplicity, value.score, value.files)
        for key, value in expected.items()
    }


@pytest.fixture(scope="module")
def authored_tasks_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create oracle-only authored-arm solutions outside checked-in fixtures."""
    root = tmp_path_factory.mktemp("authored-tasks")
    shutil.copytree(_ROOT, root, dirs_exist_ok=True)
    for source in root.glob("*/evaluation/*/solution/no-library"):
        shutil.copytree(source, source.parent / "a1")
    return root


@pytest.fixture(scope="module")
def experiment_run(
    authored_tasks_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Run the fixture experiment config end to end and return its directory."""
    output = tmp_path_factory.mktemp("experiment")
    _run(
        [
            "run",
            str(_EXPERIMENT_CONFIG),
            f"tasks_root={authored_tasks_root}",
            "--agent",
            "oracle",
            "--model",
            "dummy",
            "--output",
            str(output),
            "--n-concurrent",
            "4",
        ]
    )
    (result_path,) = tuple(output.rglob("ldb-result.json"))
    return result_path.parent


@pytest.fixture(scope="module")
def authored_run(
    authored_tasks_root: Path,
    experiment_run: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Run:
    output = tmp_path_factory.mktemp("authored")
    args = [
        "eval",
        "design",
        str(experiment_run),
        *_ORACLE,
        f"tasks_root={authored_tasks_root}",
        "--output",
        str(output),
        "--n-concurrent",
        "4",
    ]
    _run(args)
    return _only(output)


@pytest.fixture(scope="module")
def floor_run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    output = tmp_path_factory.mktemp("floor")
    _run(
        [
            "eval",
            "no-library",
            *_ORACLE,
            f"tasks_root={_ROOT}",
            "--output",
            str(output),
            "--n-concurrent",
            "4",
        ]
    )
    return _only(output)


@pytest.fixture(scope="module")
def existing_run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    output = tmp_path_factory.mktemp("existing")
    _run(
        [
            "eval",
            "existing-library",
            *_ORACLE,
            f"tasks_root={_ROOT}",
            "--task",
            "pyt",
            "--output",
            str(output),
            "--n-concurrent",
            "4",
        ]
    )
    return _only(output)


def test_experiment_end_to_end_places_evidence_and_writes_complete_result(
    experiment_run: Path,
) -> None:
    """One config produces the whole layout, every oracle cell, and a done result."""
    design = Run.open(experiment_run)
    assert isinstance(design.request(), AuthorJob)
    assert sorted(
        path.name for path in (experiment_run / "design_results").iterdir()
    ) == ["pyt__phase-1__author__a1", "rsj__phase-1__author__a1"]
    evaluation = Run.open(experiment_run / EVALUATION_RESULTS_DIR_NAME)
    assert {
        (a.task, a.problem, a.pass_rate, a.library_kind)
        for a in _report(evaluation).attempts
    } == {
        ("pyt", "01_step", 1.0, "agent"),
        ("pyt", "02_step", 1.0, "agent"),
        ("rsj", "01_step", 0.0, "agent"),
        ("rsj", "02_step", 1.0, "agent"),
    }
    result = LdbResult.load(experiment_run / "ldb-result.json")
    assert result.meta.complete is True
    assert tuple(result.implementors) == ("impl",)
    assert result.meta.outcome_counts == {
        "finished": 6,
        "reanalyze": 0,
        "reverify": 0,
        "rerun": 0,
    }
    assert {
        (row.implementor, row.task, row.problem, row.pass_rate) for row in result.trials
    } == {
        ("impl", "pyt", "01_step", 1.0),
        ("impl", "pyt", "02_step", 1.0),
        ("impl", "rsj", "01_step", 0.0),
        ("impl", "rsj", "02_step", 1.0),
    }


def test_no_library_measurement_is_exact(floor_run: Run) -> None:
    _assert_measurement(floor_run, "no-library", _FLOOR)


def test_existing_library_measurement_is_exact(existing_run: Run) -> None:
    _assert_measurement(existing_run, "existing", _CEILING)


def test_resume_recreates_only_deleted_slot(floor_run: Run, tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    shutil.copytree(floor_run.dir, backup)
    try:
        before = _report(floor_run)
        launches = floor_run.launches()
        preserved = {
            launch.trial_name: (
                floor_run.dir / launch.trial_name / "result.json"
            ).read_bytes()
            for launch in launches[1:]
        }
        deleted = floor_run.slot(launches[0]).dir
        shutil.rmtree(deleted)
        _run(["resume", str(floor_run.dir), "--n-concurrent", "4"])
        result = Run.open(floor_run.dir).slot(launches[0]).result()
        assert result is not None
        assert result.verifier_result is not None
        assert {
            name: (floor_run.dir / name / "result.json").read_bytes()
            for name in preserved
        } == preserved
        # The rerun slot reports its own usage; retained verifier metrics stay fixed.
        after = _report(Run.open(floor_run.dir))
        rerun = launches[0].trial_name
        assert tuple(
            a.model_copy(update={"usage": UsageReport()})
            if a.trial_name == rerun
            else a
            for a in after.attempts
        ) == tuple(
            a.model_copy(update={"usage": UsageReport()})
            if a.trial_name == rerun
            else a
            for a in before.attempts
        )
        assert after.model_copy(update={"attempts": ()}) == before.model_copy(
            update={"attempts": ()}
        )
    finally:
        shutil.rmtree(floor_run.dir)
        shutil.copytree(backup, floor_run.dir)


def test_replay_and_verify_task_use_persisted_commands(
    authored_run: Run, tmp_path_factory: pytest.TempPathFactory
) -> None:
    replay_output = tmp_path_factory.mktemp("replay") / "source"
    shutil.copytree(authored_run.dir, replay_output)
    _run(
        [
            "verify",
            "run",
            str(replay_output),
            "--update",
            "--n-concurrent",
            "4",
        ]
    )
    replayed = Run.open(replay_output)
    assert {(a.task, a.problem, a.pass_rate) for a in _report(replayed).attempts} == {
        ("pyt", "01_step", 1.0),
        ("pyt", "02_step", 1.0),
        ("rsj", "01_step", 0.0),
        ("rsj", "02_step", 1.0),
    }
    verification = replayed.manifest.verification
    assert verification is not None
    assert (
        verification.tasks,
        verification.problems,
        bool(verification.repo_commit),
        bool(verification.tasks_hash),
        bool(verification.source_trials),
    ) == (
        ("pyt", "rsj"),
        ("01_step", "02_step"),
        True,
        True,
        True,
    )
    verify_output = tmp_path_factory.mktemp("verify")
    _run(
        [
            "verify",
            "task",
            "pyt",
            f"tasks_root={_ROOT}",
            "--output",
            str(verify_output),
            "--n-concurrent",
            "4",
        ]
    )
    verified = _report(_only(verify_output)).attempts
    assert len(verified) == 4
    assert {(a.problem, a.library, a.pass_rate) for a in verified} == {
        ("01_step", "no-library", 1.0),
        ("02_step", "no-library", 1.0),
        ("01_step", "more-itertools", 1.0),
        ("02_step", "more-itertools", 1.0),
    }


def test_rejecting_verifier_keeps_metrics_and_zero_score(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    shutil.copytree(_ROOT, tasks)
    shutil.copyfile(
        _REJECTING, tasks / "pyt" / "evaluation" / "01_step" / "tests" / "behavior.sh"
    )
    output = tmp_path / "rejected"
    _run(
        [
            "eval",
            "no-library",
            *_ORACLE,
            f"tasks_root={tasks}",
            "--output",
            str(output),
            "--task",
            "pyt",
            "--problem",
            "01_step",
        ]
    )
    (attempt,) = _report(_only(output)).attempts
    assert (attempt.reward, _metrics(attempt.static_analysis), attempt.score) == (
        0.0,
        _FLOOR[("pyt", "01_step")].metrics,
        0.0,
    )


def test_recalculate_remeasures_to_the_same_exact_measurement(
    floor_run: Run, tmp_path: Path
) -> None:
    """Replaying saved workspaces with tests skipped reproduces every reward."""
    copy = tmp_path / "recalculated"
    shutil.copytree(floor_run.dir, copy)
    before = _report(Run.open(copy))

    _run(["recalculate", str(copy)])

    after = _report(Run.open(copy))
    assert {
        a.trial_name: (a.pass_rate, _metrics(a.static_analysis), a.score)
        for a in after.attempts
    } == {
        a.trial_name: (a.pass_rate, _metrics(a.static_analysis), a.score)
        for a in before.attempts
    }
    for attempt in after.attempts:
        assert (copy / attempt.trial_name / "verifier" / "behavior.json").is_file()


def test_static_reference_refresh_reproduces_checked_in_references(
    tmp_path: Path,
) -> None:
    """Each reference solution, measured by its task's verifier, is its reference."""
    tasks = tmp_path / "tasks"
    shutil.copytree(_ROOT, tasks)
    references = sorted(tasks.rglob("static_reference.json"))
    checked_in = {path: path.read_text() for path in references}

    _run(["static", str(tasks)])

    assert {path: path.read_text() for path in references} == checked_in
