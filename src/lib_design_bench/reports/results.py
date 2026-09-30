"""Run-level documents: the run report, the LDB result, and the experiment view."""

from __future__ import annotations

import os
import re
import statistics
from collections import defaultdict
from collections.abc import Hashable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any
from typing import get_args

import structlog
from harbor.models.trial.config import AgentConfig

from lib_design_bench.metrics.uncertainty import StratifiedEstimate
from lib_design_bench.metrics.uncertainty import clustered_mean
from lib_design_bench.metrics.uncertainty import estimate_score
from lib_design_bench.metrics.uncertainty import normalize_score_replicate
from lib_design_bench.metrics.uncertainty import score_observation
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import ReplayJob
from lib_design_bench.models.manifest import DesignLaunch
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import RunManifest
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import AgentDetails
from lib_design_bench.models.reports import DesignRow
from lib_design_bench.models.reports import EvaluationRow
from lib_design_bench.models.reports import ExecutionSummary
from lib_design_bench.models.reports import ExperimentResult
from lib_design_bench.models.reports import ImplementorAggregate
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.models.reports import LibraryAttemptAggregate
from lib_design_bench.models.reports import LibraryResult
from lib_design_bench.models.reports import OutcomeClass
from lib_design_bench.models.reports import ProblemOutcome
from lib_design_bench.models.reports import ResultMeta
from lib_design_bench.models.reports import RunReport
from lib_design_bench.models.reports import Spread
from lib_design_bench.models.reports import TrialMetric
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.reports.trials import agent_identity
from lib_design_bench.reports.trials import build_trial_report
from lib_design_bench.runs.store import EVALUATION_RESULTS_DIR_NAME
from lib_design_bench.runs.store import LDB_CONFIG_FILE_NAME
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import RunOutputConfig
from lib_design_bench.runs.store import read_authored_artifact
from lib_design_bench.runs.store import source_run_dir

logger = structlog.get_logger(__name__)


SCORE_METRICS = ("cyc_complex", "cog_complex", "halstead_volume", "sloc")
"""The ratios a score averages; each task's `tests/static_measure.py` owns them."""


def capped_simplicity(ratios: Mapping[str, float]) -> float:
    """Return the score metrics' mean after capping each ratio at 1.0.

    Reports use the same cap-before-average calculation as the score each
    task's `static_measure.py` computes. A cell without measured ratios reads
    as zero, matching the score produced when measurement was unavailable.
    """
    if all(metric in ratios for metric in SCORE_METRICS):
        return sum(min(ratios[metric], 1.0) for metric in SCORE_METRICS) / len(
            SCORE_METRICS
        )
    return 0.0


def run_report(
    manifest: RunManifest,
    launches: tuple[TrialLaunch, ...],
    trial_reports: Mapping[str, TrialReport],
    execution: ExecutionSummary | None = None,
) -> RunReport:
    """Build the complete run report from durable trial reports."""
    attempts = tuple(
        trial_reports.get(launch.trial_name) or build_trial_report(launch, None)
        for launch in launches
    )
    first = launches[0] if launches else None
    is_design = isinstance(first, DesignLaunch)
    identity = agent_identity(first.agent if first is not None else None)
    return RunReport(
        run_id=manifest.timestamp.isoformat(),
        run_type="design" if is_design else "evaluation",
        repo_commit=manifest.repo_commit,
        tasks_hash=manifest.tasks_hash,
        agent=identity["agent"],
        model=identity["model"],
        reasoning=identity["reasoning"],
        execution=execution,
        attempts=attempts,
        library_attempt_aggregates=(
            _library_attempt_aggregates(
                attempts, source_evaluation_run=manifest.timestamp.isoformat()
            )
            if not is_design
            else ()
        ),
        implementor_aggregates=(
            _implementor_aggregates(
                attempts, source_evaluation_run=manifest.timestamp.isoformat()
            )
            if not is_design
            else ()
        ),
    )


def _library_attempt_aggregates(
    attempts: Iterable[TrialReport],
    *,
    source_evaluation_run: str,
) -> tuple[LibraryAttemptAggregate, ...]:
    grouped: defaultdict[tuple[str, str, str, int | None], list[TrialReport]] = (
        defaultdict(list)
    )
    for attempt in attempts:
        grouped[
            (
                attempt.implementor,
                attempt.task,
                attempt.library,
                attempt.design_attempt,
            )
        ].append(attempt)
    aggregates = []
    for (implementor, task, library, design_attempt), group in sorted(grouped.items()):
        problems: defaultdict[str, list[TrialReport]] = defaultdict(list)
        for attempt in group:
            problems[attempt.problem].append(attempt)
        aggregates.append(
            LibraryAttemptAggregate(
                implementor=implementor,
                task=task,
                library=library,
                design_attempt=design_attempt,
                score=_score_spread(group, source_evaluation_run=source_evaluation_run),
                problems={
                    problem: _problem_outcome(rows)
                    for problem, rows in sorted(problems.items())
                },
            )
        )
    return tuple(aggregates)


def _implementor_aggregates(
    attempts: Iterable[TrialReport],
    *,
    source_evaluation_run: str,
) -> tuple[ImplementorAggregate, ...]:
    grouped: defaultdict[str, list[TrialReport]] = defaultdict(list)
    for attempt in attempts:
        grouped[attempt.implementor].append(attempt)
    return tuple(
        ImplementorAggregate(
            implementor=implementor,
            count=len(group),
            pass_rate=_spread(
                ((attempt.task, attempt.design_attempt), attempt.pass_rate or 0.0)
                for attempt in _finished(group)
            ),
            reward=_spread(
                ((attempt.task, attempt.design_attempt), attempt.reward or 0.0)
                for attempt in _finished(group)
            ),
            simplicity=_spread(
                (
                    (attempt.task, attempt.design_attempt),
                    capped_simplicity(attempt.simplicity_ratios),
                )
                for attempt in _finished(group)
            ),
            score=_score_spread(group, source_evaluation_run=source_evaluation_run),
        )
        for implementor, group in sorted(grouped.items())
    )


def _problem_outcome(attempts: Iterable[TrialReport]) -> ProblemOutcome:
    """Average one problem's finished cells, capping each cell's simplicity."""
    finished = _finished(attempts)
    if not finished:
        return ProblemOutcome(count=0)
    ratios: defaultdict[str, list[float]] = defaultdict(list)
    for attempt in finished:
        for name, value in attempt.simplicity_ratios.items():
            ratios[name].append(value)
    return ProblemOutcome(
        count=len(finished),
        pass_rate=statistics.fmean(attempt.pass_rate or 0.0 for attempt in finished),
        simplicity=statistics.fmean(
            capped_simplicity(attempt.simplicity_ratios) for attempt in finished
        ),
        simplicity_ratios={
            name: statistics.fmean(values) for name, values in sorted(ratios.items())
        },
        mean_cost_usd=statistics.fmean(
            attempt.usage.cost_usd or 0.0 for attempt in finished
        ),
    )


def _finished(attempts: Iterable[TrialReport]) -> tuple[TrialReport, ...]:
    return tuple(attempt for attempt in attempts if attempt.incomplete_reason is None)


def _score_spread(
    attempts: Iterable[TrialReport], *, source_evaluation_run: str
) -> Spread:
    """Return equal-task score evidence with finalized incompletes as zero."""
    estimate = trial_score_estimate(
        tuple(attempts), source_evaluation_run=source_evaluation_run
    )
    return Spread(
        mean=estimate.mean,
        se=estimate.se,
        n=estimate.cells,
        clusters=estimate.replicates,
        df=estimate.df,
        ci95_low=estimate.ci95_low,
        ci95_high=estimate.ci95_high,
        tasks=estimate.tasks,
    )


def trial_score_estimate(
    trials: Sequence[TrialReport], *, source_evaluation_run: str
) -> StratifiedEstimate:
    """Estimate the score of trials from one run, finalized incompletes as zero.

    Mixed or unprovenanced selections retain only a mean.
    """
    replicates = tuple(
        normalize_score_replicate(
            task=trial.task,
            library_kind=trial.library_kind,
            condition=trial.library,
            attempt=(
                trial.design_attempt if trial.library_kind == "agent" else trial.attempt
            ),
            source_design_run=trial.source_design_run,
            source_evaluation_run=source_evaluation_run,
        )
        for trial in trials
    )
    return estimate_score(
        tuple(
            score_observation(
                provenance=replicate,
                task=trial.task,
                implementor=trial.implementor,
                problem=trial.problem,
                value=0.0 if trial.incomplete_reason is not None else trial.score,
                execution=(source_evaluation_run, trial.trial_name),
            )
            for trial, replicate in zip(trials, replicates, strict=True)
        ),
        coverage_complete=True,
    )


def _spread(observations: Iterable[tuple[Hashable, float]]) -> Spread:
    estimate = clustered_mean(tuple(observations))
    return Spread(
        mean=estimate.mean,
        se=estimate.se,
        n=estimate.n,
        clusters=estimate.clusters,
    )


def _public_kwargs(values: dict[str, Any]) -> dict[str, Any]:
    """Remove credential-bearing fields recursively without dropping max_tokens."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return _public_kwargs(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return {
        key: clean(value)
        for key, value in values.items()
        if (normalized := re.sub(r"[^a-z0-9]", "", key.lower()))
        not in {
            "key",
            "secret",
            "token",
            "jwt",
            "auth",
            "authorization",
            "credential",
            "credentials",
            "env",
            "environment",
            "headers",
            "httpheaders",
        }
        and not normalized.endswith(
            ("key", "keyid", "token", "secret", "password", "credential", "credentials")
        )
    }


def agent_details(agent: AgentConfig) -> AgentDetails:
    """Keep ordinary agent kwargs, excluding credential fields and environment."""
    kwargs = agent.model_dump(mode="json")["kwargs"]
    return AgentDetails(
        agent=agent.name or agent.import_path or "unknown",
        model=agent.model_name or "unknown",
        version=kwargs.get("version"),
        reasoning=kwargs.get("reasoning_effort"),
        kwargs=_public_kwargs(
            {
                key: value
                for key, value in kwargs.items()
                if key not in ("version", "reasoning_effort")
            }
        ),
    )


def build_result(
    run: Run,
    report: RunReport,
    *,
    evaluation: tuple[Run, RunReport] | None,
    outcomes: Mapping[str, OutcomeClass],
) -> LdbResult:
    """Make the same result envelope for design, evaluation, replay, experiment.

    `outcomes` classifies every slot of `run` and of `evaluation`'s run.
    """
    request = run.request()
    source = Run.open(request.source) if isinstance(request, ReplayJob) else None
    original_launches = (
        {}
        if source is None
        else {launch.trial_name: launch for launch in source.launches()}
    )
    is_design = isinstance(
        source.request() if source is not None else request, AuthorJob
    )
    design_report = report if is_design else None
    evaluation_run, evaluation_report = (
        (run, report) if evaluation is None else evaluation
    )
    libraries: dict[str, LibraryResult] = {}
    implementors: dict[str, AgentDetails] = {}
    trials: list[TrialMetric] = []

    if design_report is not None:
        for launch, trial in zip(run.launches(), design_report.attempts, strict=True):
            if not isinstance(launch, DesignLaunch):
                raise ValueError(
                    f"Design run contains evaluation launch: {launch.trial_name}"
                )
            original = original_launches.get(launch.trial_name, launch)
            if not isinstance(original, DesignLaunch):
                raise ValueError(f"Replay changed launch kind: {launch.trial_name}")
            workspace_run = source if source is not None else run
            workspace = read_authored_artifact(workspace_run, original).workspace
            libraries[f"{launch.task.name}/a{launch.attempt}"] = LibraryResult(
                type="authored",
                task=launch.task.name,
                attempt=launch.attempt,
                path=os.path.relpath(workspace, workspace_run.dir),
                source_run=(
                    os.path.relpath(workspace_run.dir, run.dir)
                    if source is not None
                    else None
                ),
                author=agent_details(original.agent),
                score=trial.score if trial.incomplete_reason is None else None,
                input_tokens=trial.usage.input_tokens if source is None else None,
                output_tokens=trial.usage.output_tokens if source is None else None,
                input_cache_tokens=trial.usage.cache_input_tokens
                if source is None
                else None,
                elapsed=trial.usage.time_spent if source is None else None,
                steps=trial.usage.agent_steps if source is None else None,
                cost=trial.usage.cost_usd if source is None else None,
                incomplete_reason=trial.incomplete_reason,
            )

    if not is_design or evaluation is not None:
        for launch, trial in zip(
            evaluation_run.launches(), evaluation_report.attempts, strict=True
        ):
            if not isinstance(launch, EvaluationLaunch):
                raise ValueError(
                    f"Evaluation run contains design launch: {launch.trial_name}"
                )
            original = original_launches.get(launch.trial_name, launch)
            if not isinstance(original, EvaluationLaunch):
                raise ValueError(f"Replay changed launch kind: {launch.trial_name}")
            condition = launch.arm.condition
            if isinstance(condition, AuthoredArtifact):
                origin = source_run_dir(condition.source)
                path = (
                    condition.source.as_posix()
                    if origin is None
                    else os.path.relpath(condition.source, origin)
                )
                source_run = (
                    None
                    if origin is None or origin == run.dir
                    else os.path.relpath(origin, run.dir)
                )
                key = f"{launch.task.name}/a{condition.attempt}"
                saved = libraries.get(key)
                # Same-numbered attempts authored by different runs stay distinct.
                if saved is not None and (saved.path, saved.source_run) != (
                    path,
                    source_run,
                ):
                    digest = sha256(condition.source.as_posix().encode()).hexdigest()
                    key = f"{key}@{digest[:8]}"
                if key not in libraries:
                    author_request = (
                        None if origin is None else Run.open(origin).request()
                    )
                    libraries[key] = LibraryResult(
                        type="authored",
                        task=launch.task.name,
                        path=path,
                        source_run=source_run,
                        attempt=condition.attempt,
                        author=(
                            agent_details(author_request.agent)
                            if isinstance(author_request, AuthorJob)
                            else None
                        ),
                        incomplete_reason=condition.incomplete_reason,
                    )
            elif isinstance(condition, ExistingLibrary):
                key = f"{launch.task.name}/existing/{condition.name}"
                libraries.setdefault(
                    key, LibraryResult(type="existing", task=launch.task.name)
                )
            else:
                key = f"{launch.task.name}/{launch.library_name}"
                libraries.setdefault(
                    key, LibraryResult(type="no-library", task=launch.task.name)
                )
            implementors[launch.arm.label] = agent_details(original.agent)
            outcome = outcomes[launch.trial_name]
            is_replay = source is not None
            trials.append(
                TrialMetric(
                    name=launch.trial_name,
                    task=launch.task.name,
                    attempt=launch.attempt,
                    problem=launch.problem.name,
                    library=key,
                    implementor=launch.arm.label,
                    outcome=outcome,
                    simplicity=trial.simplicity,
                    pass_rate=trial.pass_rate,
                    score=trial.score,
                    input_tokens=trial.usage.input_tokens if not is_replay else None,
                    output_tokens=trial.usage.output_tokens if not is_replay else None,
                    input_cache_tokens=trial.usage.cache_input_tokens
                    if not is_replay
                    else None,
                    elapsed=trial.usage.time_spent if not is_replay else None,
                    steps=trial.usage.agent_steps if not is_replay else None,
                    cost=trial.usage.cost_usd if not is_replay else None,
                    sandbox_cost=trial.sandbox_usage.cost_usd
                    if not is_replay
                    else None,
                    incomplete_reason=trial.incomplete_reason,
                )
            )

    launches: tuple[TrialLaunch, ...] = run.launches()
    prompts = [
        path.as_posix()
        for launch in launches
        if (path := launch.prompt_relpath()) is not None
    ]
    if evaluation is not None:
        prompts.extend(
            (Path(EVALUATION_RESULTS_DIR_NAME) / path).as_posix()
            for launch in evaluation_run.launches()
            if (path := launch.prompt_relpath()) is not None
        )
    config = RunOutputConfig.load(run.dir / LDB_CONFIG_FILE_NAME)
    completed = [run.slot(launch).result() for launch in launches]
    if evaluation is not None:
        completed.extend(
            evaluation_run.slot(launch).result() for launch in evaluation_run.launches()
        )
    classes: tuple[OutcomeClass, ...] = ("finished", "reanalyze", "reverify", "rerun")
    counts: dict[str, int] = {
        name: list(outcomes.values()).count(name) for name in classes
    }
    # Only the outcome class decides completeness: a timeout or spent limit is
    # a finished outcome even though its trial records the exception.
    complete = all(outcome == "finished" for outcome in outcomes.values())
    if evaluation is not None:
        complete = complete and bool(trials)
    elif run.manifest.experiment is not None:
        complete = False
    # Verification can happen long after the agent execution; it does not
    # establish when an otherwise undated Harbor trial finished.
    finished_at = (
        max(
            result.finished_at
            for result in completed
            if result is not None and result.finished_at is not None
        )
        if complete
        and completed
        and all(
            result is not None and result.finished_at is not None
            for result in completed
        )
        else None
    )
    kind = (
        "experiment"
        if evaluation is not None or run.manifest.experiment is not None
        else "replay"
        if source is not None
        else "design"
        if is_design
        else "evaluation"
    )
    return LdbResult(
        schema_version=1,
        id=run.dir.name,
        meta=ResultMeta(
            type=kind,
            started_at=config.started_at,
            finished_at=finished_at,
            repo_commit=run.manifest.repo_commit,
            tasks_hash=run.manifest.tasks_hash,
            prompt_paths=tuple(dict.fromkeys(prompts)),
            complete=complete,
            outcome_counts=counts,
            source_run=os.path.relpath(request.source, run.dir)
            if isinstance(request, ReplayJob)
            else None,
            mode=("remeasure" if request.skip_tests else "verify")
            if isinstance(request, ReplayJob)
            else None,
        ),
        implementors=implementors,
        libraries=libraries,
        trials=tuple(trials),
    )


def build_experiment_result(
    *,
    design_report: RunReport,
    evaluation_report: RunReport | None,
    evaluation_launches: tuple[EvaluationLaunch, ...],
    outcomes: Mapping[str, OutcomeClass],
    repo_commit: str,
    tasks_hash: str,
    design_agent: AgentConfig,
    implementors: tuple[str, ...],
    design_only: bool,
) -> ExperimentResult:
    """Build the experiment result from both runs' finalized reports.

    `design_report` supplies one row per authored task and design attempt,
    carrying each named step's verifier reward. `evaluation_report` and
    `evaluation_launches` supply one row per evaluated cell; the launches carry
    the cell identity the report does not, namely the implementor key on the
    arm's label and the design attempt on its authored condition. An experiment
    whose evaluation run was never planned has no report and no cells, and is
    never complete: its cells are outstanding work rather than finished ones.

    `outcomes` is the outcome class of every design and evaluation slot, keyed
    by trial name, as `runs.outcomes` classified it from the persisted
    evidence. It decides the per-class counts and completeness, so a row here
    reports the same class resumption acts on; a trial name it does not cover
    has no persisted slot to classify and is an error.

    Score aggregates first average the problem-by-implementor cells belonging
    to one generated-library run, then runs within tasks, and finally fixed
    tasks equally. Their optional rerun intervals come from
    `metrics.uncertainty`; other measures retain descriptive clustered spreads.

    Every row carries the dollars its own trial spent, so a reader totals
    authoring cost and averages per-cell evaluation cost without reopening the
    run reports. Finalized incomplete cells retain their rows, reasons, and
    costs and contribute zero score, but no pass-rate or simplicity value.
    """
    design_rows = _design_rows(design_report, outcomes)
    evaluation_rows = (
        ()
        if evaluation_report is None
        else _evaluation_rows(evaluation_report, evaluation_launches, outcomes)
    )
    classified = [row.outcome for row in design_rows] + [
        row.outcome for row in evaluation_rows
    ]
    outcome_counts: dict[str, int] = {
        outcome: classified.count(outcome) for outcome in get_args(OutcomeClass)
    }
    complete = evaluation_report is not None and all(
        outcome == "finished" for outcome in classified
    )
    logger.debug(
        "built experiment result",
        design_rows=len(design_rows),
        evaluation_rows=len(evaluation_rows),
        outcome_counts=outcome_counts,
        complete=complete,
    )
    return ExperimentResult(
        repo_commit=repo_commit,
        tasks_hash=tasks_hash,
        design_agent=design_agent,
        implementors=implementors,
        design_only=design_only,
        complete=complete,
        outcome_counts=outcome_counts,
        design=design_rows,
        evaluation=evaluation_rows,
        library_attempt_aggregates=(
            ()
            if evaluation_report is None
            else evaluation_report.library_attempt_aggregates
        ),
        implementor_aggregates=(
            ()
            if evaluation_report is None
            else evaluation_report.implementor_aggregates
        ),
    )


def _design_rows(
    report: RunReport, outcomes: Mapping[str, OutcomeClass]
) -> tuple[DesignRow, ...]:
    """Return one row per authored task and design attempt, task-ordered."""
    rows: list[DesignRow] = []
    for attempt in report.attempts:
        if attempt.attempt is None:
            raise ValueError(
                f"design trial {attempt.trial_name!r} has no attempt number"
            )
        row = DesignRow(
            task=attempt.task,
            design_attempt=attempt.attempt,
            reward=attempt.reward,
            outcome=_outcome(outcomes, attempt.trial_name),
            cost_usd=attempt.usage.cost_usd,
        )
        logger.debug(
            "design row",
            task=row.task,
            design_attempt=row.design_attempt,
            outcome=row.outcome,
            reward=row.reward,
        )
        rows.append(row)
    return tuple(sorted(rows, key=lambda row: (row.task, row.design_attempt)))


def _evaluation_rows(
    report: RunReport,
    launches: tuple[EvaluationLaunch, ...],
    outcomes: Mapping[str, OutcomeClass],
) -> tuple[EvaluationRow, ...]:
    """Return one row per evaluated cell, keyed by implementor and both attempts."""
    reports = {attempt.trial_name: attempt for attempt in report.attempts}
    rows: list[EvaluationRow] = []
    for launch in launches:
        trial_report = reports.get(launch.trial_name)
        if trial_report is None:
            raise ValueError(
                f"evaluation report has no trial named {launch.trial_name!r}"
            )
        condition = launch.arm.condition
        if not isinstance(condition, AuthoredArtifact):
            raise ValueError(
                f"experiment cell {launch.trial_name!r} is not an authored "
                f"artifact condition: {condition.kind!r}"
            )
        row = EvaluationRow(
            implementor=launch.arm.label,
            task=launch.task.name,
            problem=launch.problem.name,
            design_attempt=condition.attempt,
            evaluation_attempt=launch.attempt,
            reward=trial_report.reward,
            pass_rate=trial_report.pass_rate,
            simplicity=trial_report.simplicity,
            simplicity_ratios=trial_report.simplicity_ratios,
            score=trial_report.score,
            outcome=_outcome(outcomes, launch.trial_name),
            incomplete_reason=trial_report.incomplete_reason,
            cost_usd=trial_report.usage.cost_usd,
        )
        logger.debug(
            "evaluation row",
            implementor=row.implementor,
            task=row.task,
            problem=row.problem,
            design_attempt=row.design_attempt,
            evaluation_attempt=row.evaluation_attempt,
            outcome=row.outcome,
            score=row.score,
            incomplete_reason=row.incomplete_reason,
        )
        rows.append(row)
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.implementor,
                row.task,
                row.problem,
                row.design_attempt,
                row.evaluation_attempt,
            ),
        )
    )


def _outcome(outcomes: Mapping[str, OutcomeClass], trial_name: str) -> OutcomeClass:
    """Return the class the slot classifier gave one planned trial."""
    outcome = outcomes.get(trial_name)
    if outcome is None:
        raise ValueError(f"no outcome was classified for trial {trial_name!r}")
    return outcome
