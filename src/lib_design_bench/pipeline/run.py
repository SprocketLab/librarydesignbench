"""Run, finalize, and lease one persisted trial-pipeline run."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path

import structlog
from harbor.models.job.config import JobConfig
from harbor.models.trial.result import TrialResult

from lib_design_bench.harbor.environments import prepare_harbor_trial_configs
from lib_design_bench.harbor.materialize import build_context
from lib_design_bench.harbor.materialize import trial_config
from lib_design_bench.harbor.runner import RETRY_CONFIG
from lib_design_bench.harbor.runner import RetainedTrialCompletion
from lib_design_bench.harbor.runner import launch_trials
from lib_design_bench.logging import run_logging
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import Request
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import RunReport
from lib_design_bench.reports.rebuild import rebuild_reports
from lib_design_bench.reports.trials import TrialPublisher
from lib_design_bench.reports.trials import build_trial_report
from lib_design_bench.reports.trials import recover_completed_verifier
from lib_design_bench.runs.outcomes import classify_run
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import HARBOR_CONFIG_FILE_NAME
from lib_design_bench.runs.store import TRIAL_RESULT_FILE_NAME
from lib_design_bench.runs.store import OutputDocument
from lib_design_bench.runs.store import Run

logger = structlog.get_logger(__name__)


_LEASE_FILE = ".resume.lock"


def run(
    launches: tuple[TrialLaunch, ...],
    run_dir: Path,
    *,
    n_concurrent: int,
    debug_build_contexts: bool,
    runner: asyncio.Runner | None = None,
    progress_source: Run | None = None,
) -> None:
    """Materialize and launch planned cells without publishing a run report."""
    try:
        if not launches:
            return
        # A trial that runs its tests is scored against its problem's reference,
        # so a missing one fails before any agent runs. A skip-tests trial only
        # measures, which is also how a first reference is made.
        for launch in launches:
            if (
                isinstance(launch, EvaluationLaunch)
                and launch.verifier_env.get("LDB_SKIP_TESTS") != "1"
            ):
                launch.static_reference()
        persisted = Run.open(run_dir)
        request = persisted.request()
        agents = {
            launch.agent.model_dump_json(): launch.agent
            for launch in persisted.launches()
        }
        # Harbor's job config shape; each slot's own config.json holds its task.
        job_config = JobConfig(
            job_name=run_dir.name,
            jobs_dir=run_dir.parent,
            n_concurrent_trials=n_concurrent,
            retry=RETRY_CONFIG,
            environment=request.environment,
            agents=list(agents.values()),
        )
        (run_dir / HARBOR_CONFIG_FILE_NAME).write_text(
            job_config.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        retained_completions = _retained_trial_completions(
            persisted if progress_source is None else progress_source,
            launches,
        )
        previous_usage = {
            launch.trial_name: trial.usage
            for launch in launches
            if (trial := persisted.slot(launch).trial_report()) is not None
        }
        persisted.unpublish_result()
        for launch in launches:
            persisted.slot(launch).clear()
        runnable: list[TrialLaunch] = []
        for launch in launches:
            if (
                isinstance(launch, EvaluationLaunch)
                and isinstance(condition := launch.arm.condition, AuthoredArtifact)
                and condition.incomplete_reason is not None
            ):
                _log_missing_authored_library(launch, condition)
                continue
            runnable.append(launch)
        logger.info(
            "Preparing Docker images and trial build contexts.",
            run_dir=run_dir.as_posix(),
            planned_count=len(launches),
            runnable_count=len(runnable),
            skipped_count=len(launches) - len(runnable),
        )
        if not runnable:
            return
        trials_dir = Path(tempfile.mkdtemp(prefix=".harbor-", dir=run_dir))
        contexts: dict[str, Path] = {}
        for launch in runnable:
            if launch.build_context_key not in contexts:
                contexts[launch.build_context_key] = build_context(launch, run_dir)
        logger.info(
            "Materialized shared build contexts.",
            run_dir=run_dir.as_posix(),
            context_count=len(contexts),
            cell_count=len(runnable),
        )
        configs = prepare_harbor_trial_configs(
            tuple(
                trial_config(
                    launch,
                    run_dir=run_dir,
                    task_dir=contexts[launch.build_context_key],
                    trials_dir=trials_dir,
                )
                for launch in runnable
            )
        )
        publisher = TrialPublisher(
            run=persisted,
            launches={launch.trial_name: launch for launch in runnable},
            previous_usage=previous_usage,
        )
        launch = launch_trials(
            configs,
            run_dir=run_dir,
            setup=_setup_lines(request, runnable),
            n_concurrent=n_concurrent,
            completion_processor=publisher.publish,
            retained_completions=retained_completions,
        )
        if runner is None:
            asyncio.run(launch)
        else:
            runner.run(launch)
    finally:
        # Harbor's scratch trial directories are spent once the batch ends:
        # every finished trial has moved into its slot.
        for scratch in run_dir.glob(".harbor-*"):
            shutil.rmtree(scratch)
        if not debug_build_contexts:
            build_contexts = run_dir / "build-contexts"
            if build_contexts.exists():
                shutil.rmtree(build_contexts)


def execute(
    request: Request,
    run_dir: Path,
    *,
    force: bool,
    debug_build_contexts: bool,
    runner: asyncio.Runner | None = None,
    progress_source: Run | None = None,
) -> RunReport:
    """Plan, run, and finalize one request's run under its writer lease."""
    with lease(run_dir, force=force):
        run(
            plan(request, run_dir),
            run_dir,
            n_concurrent=request.n_concurrent,
            debug_build_contexts=debug_build_contexts,
            runner=runner,
            progress_source=progress_source,
        )
        return finalize(run_dir)


def _retained_trial_completions(
    persisted: Run, launches: tuple[TrialLaunch, ...]
) -> tuple[RetainedTrialCompletion, ...]:
    """Rebuild live metrics for finished slots outside this Harbor batch."""
    progress_run = persisted
    scheduled = {launch.trial_name for launch in launches}
    outcomes = classify_run(progress_run)
    retained: list[RetainedTrialCompletion] = []
    for launch in progress_run.launches():
        if launch.trial_name in scheduled or outcomes[launch.trial_name] != "finished":
            continue
        slot = progress_run.slot(launch)
        result = slot.result()
        if result is None:
            raise ValueError(f"Finished slot has no result: {slot.dir}")
        recovered = recover_completed_verifier(result, slot.dir)
        retained.append(
            RetainedTrialCompletion(
                trial_name=launch.trial_name,
                result=recovered,
                trial=build_trial_report(launch, recovered),
            )
        )
    return tuple(retained)


def _setup_lines(request: Request, launches: list[TrialLaunch]) -> tuple[str, ...]:
    """Name each distinct model setup a batch launches, for the live panel."""
    if not isinstance(request, (Job, AuthorJob)):
        return ()
    setups = dict.fromkeys(
        (
            launch.agent.model_name or "—",
            launch.agent.kwargs.get("reasoning_effort", "—"),
        )
        for launch in launches
    )
    return tuple(
        f"Model {model}  ·  Reasoning {reasoning}" for model, reasoning in setups
    )


def _log_missing_authored_library(
    launch: EvaluationLaunch, condition: AuthoredArtifact
) -> None:
    """Record all source evidence for a deliberately unlaunched authored cell."""
    logger.error(
        "Skipping Evaluation Phase cell with unavailable Design Phase library.",
        task=launch.task.name,
        author_attempt=condition.attempt,
        expected_artifact_path=condition.source.as_posix(),
        affected_problem_count=len(condition.problems),
        incomplete_reason=condition.incomplete_reason,
    )


def finalize(run_dir: Path) -> RunReport:
    """Publish a complete report from persisted slots, including empty runs.

    Every derived document is rewritten through `reports.rebuild.rebuild_reports`,
    the one regeneration path, so finalizing an experiment's evaluation child
    also republishes the experiment root's result on the current models.
    """
    persisted = Run.open(run_dir)
    launches = persisted.launches()
    logger.debug(
        "Finalizing planned trial cells.",
        run_dir=run_dir.as_posix(),
        planned_count=len(launches),
    )
    results_by_name: dict[str, TrialResult | None] = {}
    recovered_documents = []
    for launch in launches:
        slot = persisted.slot(launch)
        result = slot.result()
        if result is not None:
            recovered = recover_completed_verifier(result, slot.dir)
            if recovered is not result:
                recovered_documents.append(
                    OutputDocument(
                        slot.dir / TRIAL_RESULT_FILE_NAME,
                        recovered.model_dump_json(indent=4),
                    )
                )
            result = recovered
        results_by_name[launch.trial_name] = result
    report = rebuild_reports(
        persisted, results_by_name, tuple(recovered_documents), repricing=None
    ).report
    logger.debug(
        "Finalized run report.",
        run_dir=run_dir.as_posix(),
        run_id=report.run_id,
        trial_count=len(report.attempts),
        completed_result_count=sum(
            result is not None for result in results_by_name.values()
        ),
    )
    return report


def leased(run_dir: Path) -> bool:
    """Report whether one run directory currently holds a writer lease."""
    return (run_dir / _LEASE_FILE).is_file()


@contextmanager
def lease_subtree(run_dir: Path, *, force: bool):
    """Lease every run whose reports this resume can mutate, root first."""
    with _logged_lease(run_dir, _subtree_lease_paths(run_dir), force=force):
        yield


def _subtree_lease_paths(run_dir: Path) -> tuple[Path, ...]:
    """Return the root and child paths in the shared acquisition order."""
    owner = Run.open(run_dir).result_owner()
    child = owner.evaluation_child()
    return (owner.dir,) if child is None else (owner.dir, child.dir)


@contextmanager
def lease(run_dir: Path, *, force: bool):
    """Hold the run-local single-writer lease for planning and mutation."""
    with _logged_lease(run_dir, (run_dir,), force=force):
        yield


@contextmanager
def _logged_lease(run_dir: Path, paths: tuple[Path, ...], *, force: bool):
    """Hold one or more writer locks while logging the requested run."""
    with _acquire_leases(paths, force=force), run_logging(run_dir):
        logger.info("Run started.", run_dir=run_dir.as_posix())
        try:
            yield
        except BaseException:
            logger.error("Run failed or interrupted.", exc_info=True)
            raise
        else:
            logger.info("Run finished.", run_dir=run_dir.as_posix())


@contextmanager
def _acquire_leases(paths: tuple[Path, ...], *, force: bool):
    """Create a preflighted group of leases without nesting acquisitions."""
    if not force:
        for path in paths:
            if leased(path):
                raise ValueError(
                    f"Run resume lease already exists: {path / _LEASE_FILE}. Use "
                    "`--force` only after confirming no process is mutating the run."
                )
    acquired: list[Path] = []
    try:
        for run_dir in paths:
            run_dir.mkdir(parents=True, exist_ok=True)
            lease_path = run_dir / _LEASE_FILE
            if force:
                lease_path.unlink(missing_ok=True)
            try:
                descriptor = os.open(
                    lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError as error:
                raise ValueError(
                    f"Run resume lease already exists: {lease_path}. Use `--force` only "
                    "after confirming no process is mutating the run."
                ) from error
            acquired.append(lease_path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    f"pid={os.getpid()}\nhost={socket.gethostname()}\n"
                    f"started={datetime.now(UTC).isoformat()}\n"
                )
        yield
    finally:
        for lease_path in acquired:
            lease_path.unlink(missing_ok=True)
