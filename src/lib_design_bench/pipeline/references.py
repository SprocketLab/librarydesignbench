"""Remeasure every checked-in static reference through its task's verifier."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog
from harbor.models.trial.config import EnvironmentConfig

from lib_design_bench.models import Arm
from lib_design_bench.models import Job
from lib_design_bench.models import NoLibrary
from lib_design_bench.models import Problem
from lib_design_bench.models import StaticMetrics
from lib_design_bench.models import StaticReference
from lib_design_bench.models import Task
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.job import AgentConfig
from lib_design_bench.models.job import arm_label
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.reports import FORMAT_FAILURE_LOG
from lib_design_bench.models.reports import MEASURE_FAILURE_LOG
from lib_design_bench.pipeline.run import lease
from lib_design_bench.pipeline.run import run
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import OutputDocument
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import write_run_output_documents

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class StaticReferenceRow:
    """One refreshed problem reference and its display identity."""

    task: str
    problem: str
    library: str
    metrics: StaticMetrics


def refresh_static_references(
    directory: Path,
    *,
    oracle: AgentConfig,
    n_concurrent: int,
    environment: EnvironmentConfig,
) -> tuple[StaticReferenceRow, ...]:
    """Remeasure every problem's reference solution below recursively found tasks.

    Each problem's reference library, the one its `static_reference.json`
    names (or, for a problem without one yet, the first existing library it
    has a solution for), runs as an `oracle` trial whose verifier skips the
    behavioral tests, so the reference is measured by the task's own
    `static_measure.py` exactly as a submission is. Every reference that measured is written; the trials
    of any that did not are kept for inspection and named in the error.
    """
    tasks = tuple(
        Task.from_dir(path.parent) for path in sorted(directory.rglob("task.yaml"))
    )
    if not tasks:
        raise ValueError(f"No tasks found below directory: {directory}")
    problems = tuple(problem for task in tasks for problem in task.active_problems())
    libraries = {
        (problem.task.source_dir, problem.name): _reference_library(problem)
        for problem in problems
    }
    label = arm_label(oracle)
    arms = []
    for task in tasks:
        by_library: dict[str, list[str]] = {}
        for problem in task.active_problems():
            library = libraries[(task.source_dir, problem.name)]
            by_library.setdefault(library, []).append(problem.name)
        for library, names in by_library.items():
            condition = (
                NoLibrary(problems=tuple(names))
                if library == "no-library"
                else ExistingLibrary(
                    task=task,
                    name=library,
                    entry=task.existing_libraries[library],
                    problems=tuple(names),
                )
            )
            arms.append(Arm(label=label, condition=condition, agent=oracle))
    request = Job(
        problems=problems,
        arms=tuple(arms),
        n_concurrent=n_concurrent,
        environment=environment,
        verifier_env={"LDB_SKIP_TESTS": "1"},
    )
    rows: list[StaticReferenceRow] = []
    failures: list[str] = []
    run_dir = Path(tempfile.mkdtemp(prefix="ldb-static-references-"))
    with lease(run_dir, force=False):
        launches = plan(request, run_dir)
        run(launches, run_dir, n_concurrent=n_concurrent, debug_build_contexts=False)
    measured = Run.open(run_dir)
    # A no-library condition is not task-scoped, so it can also run a
    # same-named problem of another task; only the reference arm counts.
    for launch in launches:
        if (
            not isinstance(launch, EvaluationLaunch)
            or launch.library_name
            != (libraries[(launch.task.source_dir, launch.problem.name)])
        ):
            continue
        verifier_dir = measured.slot(launch).dir / "verifier"
        metrics_path = verifier_dir / "static_metrics.json"
        if not metrics_path.is_file():
            logs = " ".join(
                (verifier_dir / name).read_text(encoding="utf-8")[-500:]
                for name in (FORMAT_FAILURE_LOG, MEASURE_FAILURE_LOG)
                if (verifier_dir / name).is_file()
            )
            failures.append(
                f"{launch.task.name}/{launch.problem.name}: "
                f"{logs or 'the verifier wrote no measurement'}"
            )
            continue
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        if metrics["parse_recovered"]:
            logger.warning(
                "Measured a recovered reference parse tree",
                task=launch.task.name,
                problem=launch.problem.name,
                parse_error_nodes=metrics["parse_error_nodes"],
            )
        rows.append(
            StaticReferenceRow(
                task=launch.task.name,
                problem=launch.problem.name,
                library=launch.library_name,
                metrics=StaticMetrics.model_validate(
                    {name: metrics[name] for name in StaticMetrics.scalar_names()}
                ),
            )
        )
    by_name = {(problem.task.name, problem.name): problem for problem in problems}
    write_run_output_documents(
        tuple(
            OutputDocument(
                by_name[(row.task, row.problem)].dir
                / "tests"
                / "static_reference.json",
                StaticReference(
                    library=row.library, metrics=row.metrics
                ).model_dump_json(indent=2)
                + "\n",
            )
            for row in rows
        )
    )
    logger.debug("Wrote static references", reference_count=len(rows))
    if failures:
        raise ValueError(
            f"Wrote {len(rows)} static references; these could not be measured, "
            f"and their trials are kept in {run_dir}: " + "; ".join(failures)
        )
    shutil.rmtree(run_dir)
    return tuple(rows)


def _reference_library(problem: Problem) -> str:
    """Return the library a problem's reference names, or the first one solved.

    A problem without a reference yet takes the first declared existing library
    it has a checked-in solution for, which the refresh then measures.
    """
    reference = problem.static_reference()
    if reference is not None:
        return reference.library
    for library in problem.task.existing_libraries:
        if problem.solution_dir(library).is_dir():
            return library
    raise ValueError(
        "Problem has no static reference and no existing-library solution to "
        f"measure one from: {problem.task.name}/{problem.name}"
    )
