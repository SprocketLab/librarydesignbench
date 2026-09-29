"""Verifier replay over saved slots, remeasurement, and recalculation of saved runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog
from harbor.models.trial.config import EnvironmentConfig
from harbor.models.trial.config import SourceTrialConfig

from lib_design_bench.metrics.costs import CostRates
from lib_design_bench.metrics.costs import ModelPricing
from lib_design_bench.metrics.costs import Pricing
from lib_design_bench.models import RunReport
from lib_design_bench.models import VerificationRecord
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import ReplayJob
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import RunManifest
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import FORMAT_FAILURE_LOG
from lib_design_bench.models.reports import MEASURE_FAILURE_LOG
from lib_design_bench.pipeline.run import execute
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.pipeline.run import run
from lib_design_bench.reports.rebuild import RebuiltReports
from lib_design_bench.reports.rebuild import rebuild_reports
from lib_design_bench.reports.trials import build_trial_report
from lib_design_bench.runs.outcomes import VERIFIER_EXCEPTIONS
from lib_design_bench.runs.outcomes import classify
from lib_design_bench.runs.outcomes import launches_in
from lib_design_bench.runs.plan import hash_tasks
from lib_design_bench.runs.plan import repo_commit
from lib_design_bench.runs.store import MANIFEST_FILE_NAME
from lib_design_bench.runs.store import TRIAL_REPORT_FILE_NAME
from lib_design_bench.runs.store import TRIAL_RESULT_FILE_NAME
from lib_design_bench.runs.store import OutputDocument
from lib_design_bench.runs.store import OutputTree
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import Slot
from lib_design_bench.runs.store import replace_run_output_documents
from lib_design_bench.runs.store import verifier_rewards

logger = structlog.get_logger(__name__)


MEASUREMENT_REVISION = 1
"""Revision of the static-analysis and reward contract of `static_measure.py`."""


_MEASUREMENT_LOGS = (
    "reward.json",
    "reward.txt",
    "behavior.json",
    "static_metrics.json",
    FORMAT_FAILURE_LOG,
    MEASURE_FAILURE_LOG,
    "reward.log",
)
"""What a task's `test.sh` writes when it measures, and a remeasure replaces."""


def settle(
    launches: tuple[TrialLaunch, ...],
    run_dir: Path,
    *,
    n_concurrent: int,
    environment: EnvironmentConfig,
    debug_build_contexts: bool,
    runner: asyncio.Runner,
) -> RunReport:
    """Run launches, replay what only failed grading or measurement, and finalize."""
    run(
        launches,
        run_dir,
        n_concurrent=n_concurrent,
        debug_build_contexts=debug_build_contexts,
        runner=runner,
    )
    reverify(
        Run.open(run_dir),
        n_concurrent=n_concurrent,
        environment=environment,
        runner=runner,
    )
    remeasure(
        Run.open(run_dir),
        n_concurrent=n_concurrent,
        environment=environment,
        runner=runner,
    )
    return finalize(run_dir)


def reverify(
    source: Run,
    *,
    n_concurrent: int,
    environment: EnvironmentConfig,
    runner: asyncio.Runner | None = None,
) -> tuple[TrialLaunch, ...]:
    """Replay the verifier for saved slots whose only failure was grading.

    No agent runs: each re-verify slot's collected artifacts are graded again
    by the current checked-in tests in a throwaway run beside the source, and
    the verifier result and its provenance are merged back into the source
    slots exactly as `ldb verify run --update` merges them. The run itself
    selects those slots, so a run with none is finished work rather than a
    caller error. Returns the slots that were replayed.
    """
    return _replay_selected(
        source,
        launches_in(source, "reverify"),
        skip_tests=False,
        n_concurrent=n_concurrent,
        environment=environment,
        runner=runner,
    )


def remeasure(
    source: Run,
    *,
    n_concurrent: int,
    environment: EnvironmentConfig,
    runner: asyncio.Runner | None = None,
) -> tuple[TrialLaunch, ...]:
    """Remeasure graded slots whose static measurement never ran or is stale.

    Each task's verifier owns measurement, so a slot is remeasured by replaying
    its saved workspace with the behavioral tests skipped and its recorded
    counts scored instead. A measurement is stale when it predates
    `MEASUREMENT_REVISION` or the problem's current static reference. Returns
    the slots that were replayed.
    """
    launches = tuple(
        launch
        for launch in source.launches()
        if isinstance(launch, EvaluationLaunch)
        and _needs_measurement(source.slot(launch), launch)
    )
    return _replay_selected(
        source,
        launches,
        skip_tests=True,
        n_concurrent=n_concurrent,
        environment=environment,
        runner=runner,
    )


def remeasurable(slot: Slot) -> bool:
    """Return whether a slot kept the counts and workspace a remeasure replays."""
    return slot.behavioral_counts() is not None and slot.workspace_error() is None


def _needs_measurement(slot: Slot, launch: EvaluationLaunch) -> bool:
    if not remeasurable(slot):
        return False
    outcome = classify(slot).outcome
    return outcome == "reanalyze" or (
        outcome == "finished" and not _measurement_is_current(slot, launch)
    )


def _measurement_is_current(slot: Slot, launch: EvaluationLaunch) -> bool:
    """Return whether a slot's reward used the current metric contract."""
    rewards = verifier_rewards(slot.result())
    return rewards.get("measurement_revision") == MEASUREMENT_REVISION and rewards.get(
        "reference_identity"
    ) == (reference_identity(launch.static_reference().metrics.as_scalars()))


def reference_identity(reference: Mapping[str, int | float]) -> int:
    """Return the identity `static_measure.py` records for the reference it used."""
    encoded = json.dumps(dict(sorted(reference.items())), separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(encoded.encode()).digest()[:6], "big")


def _replay_selected(
    source: Run,
    launches: tuple[TrialLaunch, ...],
    *,
    skip_tests: bool,
    n_concurrent: int,
    environment: EnvironmentConfig,
    runner: asyncio.Runner | None,
) -> tuple[TrialLaunch, ...]:
    """Replay the selected slots and merge the results into their source run.

    A replay that fails before it can grade, such as task materialization
    reading a source file that no longer exists, is logged and leaves the
    slots in their class for the next resume.
    """
    trials = tuple(launch.trial_name for launch in launches)
    if not trials:
        logger.debug("No saved slot needs a replay.", run_dir=source.dir.as_posix())
        return ()
    logger.info(
        "Replaying saved slots.",
        run_dir=source.dir.as_posix(),
        skip_tests=skip_tests,
        trial_count=len(trials),
        trials=list(trials),
    )
    try:
        replay(
            source,
            trials,
            skip_tests=skip_tests,
            n_concurrent=n_concurrent,
            environment=environment,
            runner=runner,
        )
    except (OSError, ValueError):
        # The source slots were not touched, so they stay in their class and
        # the run's report still says what they need. Failing the whole run
        # here would discard every finished slot's report.
        logger.error(
            "Replay failed before any source slot changed; `ldb resume` will "
            "replay these slots again.",
            run_dir=source.dir.as_posix(),
            trial_count=len(trials),
            trials=list(trials),
            exc_info=True,
        )
        return ()
    logger.info(
        "Merged replayed verifier results into their source slots.",
        run_dir=source.dir.as_posix(),
        trial_count=len(trials),
    )
    return launches


def replay(
    source: Run,
    trials: tuple[str, ...],
    *,
    skip_tests: bool,
    n_concurrent: int,
    environment: EnvironmentConfig,
    runner: asyncio.Runner | None,
) -> RunReport:
    """Replay the named slots of `source` and merge the results into it."""
    return replay_and_update(
        ReplayJob(
            source=source.dir.resolve(),
            trials=trials,
            n_concurrent=n_concurrent,
            environment=environment,
            skip_tests=skip_tests,
        ),
        source,
        force=False,
        runner=runner,
    )


def replay_and_update(
    request: ReplayJob,
    source: Run,
    *,
    force: bool,
    runner: asyncio.Runner | None = None,
) -> RunReport:
    """Replay one request in a throwaway run and commit it to its source run.

    The replay run is temporary because its only durable product is the
    verifier evidence merged into the source slots; nothing else about it is
    worth keeping beside the run it regraded.
    """
    with TemporaryDirectory(
        prefix=f".{source.dir.name}.verify-", dir=source.dir.parent
    ) as temporary:
        replay_dir = Path(temporary)
        execute(
            request,
            replay_dir,
            force=force,
            debug_build_contexts=False,
            runner=runner,
            progress_source=source,
        )
        return update_source(source, Run.open(replay_dir))


def update_source(source: Run, replay: Run) -> RunReport:
    """Merge replay verifier evidence into the source slots it regraded.

    A replay that failed outside verification observed nothing about the agent
    work it was given, so merging it would replace intact source evidence with
    the replay's own infrastructure failure. Those slots keep what their run
    saved and stay outstanding for the next resume.
    """
    request = replay.request()
    remeasured = isinstance(request, ReplayJob) and request.skip_tests
    selected = tuple(
        launch
        for launch in replay.launches()
        if _regraded(source, replay, launch, remeasured=remeasured)
    )
    if not selected:
        return finalize(source.dir)
    documents = _source_documents(source, replay, selected, remeasured=remeasured)
    # A published scoreboard may not outlive evidence being regraded. If the
    # merge is interrupted, finalize can rebuild it from retained trial slots.
    source.unpublish_result()
    replace_run_output_documents(
        documents,
        completion_marker=source.dir / MANIFEST_FILE_NAME,
        trees=_verifier_trees(source, replay, selected, remeasured=remeasured),
    )
    return finalize(source.dir)


def _regraded(
    source: Run, replay: Run, launch: TrialLaunch, *, remeasured: bool
) -> bool:
    """Return whether one replay graded the saved artifacts it was given.

    A verification failure is an outcome of that work: the current tests reached
    it and timed out or could not be scored. Every other exception, and a replay
    holding no verifier result at all, describes the replay's own execution. A
    remeasure only rescores counts the slot already holds, so any exception
    leaves the slot's own evidence in place.
    """
    verified = replay.slot(launch).result()
    exception = None if verified is None else verified.exception_info
    if exception is None:
        graded = verified is not None and verified.verifier_result is not None
    else:
        graded = not remeasured and exception.exception_type in VERIFIER_EXCEPTIONS
    if graded:
        return True
    logger.warning(
        "Discarded a verifier replay that graded nothing; its source slot keeps "
        "the evidence it saved and still needs resolving.",
        run_dir=source.dir.as_posix(),
        trial=launch.trial_name,
        replay_exception=None if exception is None else exception.exception_type,
        replay_exception_message=(
            None if exception is None else exception.exception_message
        ),
    )
    return False


def _source_documents(
    source: Run,
    replay: Run,
    selected: tuple[TrialLaunch, ...],
    *,
    remeasured: bool,
) -> tuple[OutputDocument, ...]:
    # The manifest is the update's completion marker; only a re-grade records
    # verification provenance, since a remeasure ran none of the tests.
    manifest = (
        source.manifest
        if remeasured
        else RunManifest.model_copy(
            source.manifest,
            update={"verification": _verification_record(source, selected)},
        )
    )
    documents: list[OutputDocument] = [
        OutputDocument(
            source.dir / MANIFEST_FILE_NAME, manifest.model_dump_json(indent=2) + "\n"
        ),
    ]
    for launch in selected:
        source_slot = source.slot(launch)
        replay_slot = replay.slot(launch)
        original = source_slot.result()
        verified = replay_slot.result()
        if original is None:
            raise ValueError(
                f"Selected source trial result does not exist: {source_slot.dir}"
            )
        if verified is None:
            raise ValueError(f"Replay result does not exist: {replay_slot.dir}")
        # A remeasure replaces only the reward: the agent's own exception,
        # such as the limit that ended it, is still that trial's outcome.
        merged = original.model_copy(
            update={"verifier_result": verified.verifier_result}
            if remeasured
            else {
                "verifier_result": verified.verifier_result,
                "exception_info": verified.exception_info,
                "verifier": verified.verifier,
            }
        )
        documents.append(
            OutputDocument(
                source_slot.dir / TRIAL_RESULT_FILE_NAME,
                merged.model_dump_json(indent=2) + "\n",
            )
        )
        previous = source_slot.trial_report()
        trial_report = build_trial_report(launch, merged)
        if previous is not None:
            # Regrading replaces verifier evidence, not the agent's historical
            # pricing policy or recorded API spend.
            trial_report = trial_report.model_copy(update={"usage": previous.usage})
        documents.append(
            OutputDocument(
                source_slot.dir / TRIAL_REPORT_FILE_NAME,
                trial_report.model_dump_json(indent=2) + "\n",
            )
        )
    return tuple(documents)


def _verifier_trees(
    source: Run,
    replay: Run,
    selected: tuple[TrialLaunch, ...],
    *,
    remeasured: bool,
) -> tuple[OutputTree, ...]:
    """Stage each replayed verifier directory for its source slot.

    A remeasure ran no behavioral tests, so its tree keeps the slot's own
    verifier logs and replaces only the measurement `test.sh` writes.
    """
    trees = []
    for launch in selected:
        replayed = replay.slot(launch).dir / "verifier"
        if not replayed.is_dir():
            continue
        recorded = source.slot(launch).dir / "verifier"
        if remeasured:
            merged = replay.slot(launch).dir / "verifier-remeasured"
            if recorded.is_dir():
                shutil.copytree(
                    recorded, merged, ignore=shutil.ignore_patterns(*_MEASUREMENT_LOGS)
                )
            shutil.copytree(replayed, merged, dirs_exist_ok=True)
            replayed = merged
        trees.append(OutputTree(replayed, recorded))
    return tuple(trees)


def _verification_record(
    source: Run, selected: tuple[TrialLaunch, ...]
) -> VerificationRecord:
    tasks = tuple({launch.task.source_dir: launch.task for launch in selected}.values())
    return VerificationRecord(
        timestamp=datetime.now(UTC),
        repo_commit=repo_commit(),
        tasks_hash=hash_tasks(tasks),
        source_trials=tuple(
            SourceTrialConfig(
                action="regrade", type="local", path=source.slot(launch).dir
            )
            for launch in selected
        ),
        tasks=tuple(task.name for task in tasks),
        problems=tuple(
            dict.fromkeys(
                launch.problem.name
                for launch in selected
                if isinstance(launch, EvaluationLaunch)
            )
        ),
    )


def recalculate(
    target: Path,
    pricing: Pricing | None,
    *,
    n_concurrent: int,
    environment: EnvironmentConfig,
) -> tuple[RebuiltReports, ...]:
    """Remeasure every graded slot of one saved run or experiment and rebuild reports.

    Each Evaluation Phase slot that kept its behavioral counts and workspace is
    replayed in `environment` through its task's verifier with the behavioral
    tests skipped, so
    the reward is rebuilt from a fresh measurement and the recorded counts. A
    slot without them keeps the reward its run recorded. An experiment
    directory recalculates its design run and direct evaluation child in order,
    so the child republishes the experiment result from current design evidence.
    """
    root = Run.open(target)
    request = root.request()
    if isinstance(request, AuthorJob):
        child = root.evaluation_child()
        runs = (root,) if child is None else (root, child)
    elif isinstance(request, Job):
        runs = (root,)
    else:
        raise ValueError(f"Expected a design or evaluation run: {root.dir}")
    repricing = _repricing_by_run(runs, pricing)
    rebuilt = []
    for saved in runs:
        if isinstance(saved.request(), Job):
            _remeasure_all(saved, n_concurrent=n_concurrent, environment=environment)
            saved = Run.open(saved.dir)
        rebuilt.append(
            rebuild_reports(
                saved,
                {
                    launch.trial_name: saved.slot(launch).result()
                    for launch in saved.launches()
                },
                (),
                repricing=None if repricing is None else repricing[saved.dir],
            )
        )
    return tuple(rebuilt)


def _remeasure_all(
    run: Run, *, n_concurrent: int, environment: EnvironmentConfig
) -> None:
    """Replay every remeasurable Evaluation Phase slot with its tests skipped."""
    trials = []
    for launch in run.launches():
        if not isinstance(launch, EvaluationLaunch):
            continue
        if remeasurable(run.slot(launch)):
            trials.append(launch.trial_name)
        else:
            logger.warning(
                "Keeping the recorded reward of a slot without behavioral counts "
                "or a retained workspace.",
                run_dir=run.dir.as_posix(),
                trial_name=launch.trial_name,
            )
    if trials:
        replay(
            run,
            tuple(trials),
            skip_tests=True,
            n_concurrent=n_concurrent,
            environment=environment,
            runner=None,
        )


def _repricing_by_run(
    runs: tuple[Run, ...], pricing: Pricing | None
) -> dict[Path, dict[str, CostRates]] | None:
    """Select the rates each run's trials are repriced at, keyed by trial name.

    Both policies are resolved against the planned launches before any slot is
    measured, so a selector that names nothing the target ran fails before the
    first document is rewritten.
    """
    if pricing is None:
        return None
    if isinstance(pricing, ModelPricing):
        return _model_repricing(runs, pricing)
    evaluation_launches = tuple(
        launch
        for run in runs
        for launch in run.launches()
        if isinstance(launch, EvaluationLaunch)
    )
    is_experiment = len(runs) == 2 and isinstance(runs[0].request(), AuthorJob)
    if is_experiment and pricing.implementor is None:
        raise ValueError("Priced experiment recalculation requires --implementor")
    available = sorted({launch.arm.label for launch in evaluation_launches})
    if pricing.implementor is not None and pricing.implementor not in available:
        choices = ", ".join(available) if available else "none"
        raise ValueError(
            f"Unknown implementor {pricing.implementor!r}; "
            f"available implementors: {choices}"
        )
    return {
        run.dir: {
            launch.trial_name: pricing.rates
            for launch in run.launches()
            if isinstance(launch, EvaluationLaunch)
            and (pricing.implementor is None or launch.arm.label == pricing.implementor)
        }
        for run in runs
    }


def _model_repricing(
    runs: tuple[Run, ...], pricing: ModelPricing
) -> dict[Path, dict[str, CostRates]]:
    """Price every Design and Evaluation Phase cell whose model the policy declares.

    A config is written once and reused across runs, so it may name models a
    target never ran; naming none of them is instead a selector that would
    silently reprice nothing, and the models the target did run are listed.
    """
    repricing = {
        run.dir: {
            launch.trial_name: rates
            for launch in run.launches()
            if (rates := pricing.for_model(launch.agent.model_name)) is not None
        }
        for run in runs
    }
    if any(repricing.values()):
        return repricing
    available = sorted(
        {
            model
            for run in runs
            for launch in run.launches()
            if (model := launch.agent.model_name) is not None
        }
    )
    choices = ", ".join(available) if available else "none"
    raise ValueError(
        f"Pricing config prices no model this target ran; models run: {choices}"
    )
