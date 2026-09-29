"""The only inferential estimators for benchmark measures.

`stratified_mean` owns the benchmark score estimator. It treats a task as a
fixed stratum and one independently executed library condition as a replicate:
all problem-by-implementor cells first average within a library run, library
runs average within a task, and task means average equally. Its
confidence interval therefore
concerns expected performance under the selected fixed benchmark and execution
protocol; it is not a prediction interval or evidence about new tasks.

`clustered_mean` estimates descriptive non-score measures. No other module may
calculate a standard error, standard deviation, or confidence interval of a
benchmark measure.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
from enum import StrEnum
from math import exp
from math import fsum
from math import isfinite
from math import lgamma
from math import log
from math import sqrt
from statistics import NormalDist
from statistics import variance


@dataclass(frozen=True, slots=True)
class StratifiedObservation:
    """One problem-by-implementor score belonging to one complete library run.

    `replicate` must name the condition-specific independent execution, not a
    bare attempt number. Every cell in a replicate first forms that library's
    score; runs then average within tasks and tasks average equally.
    `implementor` and `problem` identify the logical fixed downstream panel.
    `cell` optionally distinguishes repeated executions of that logical cell;
    execution provenance must not change which problems an implementor covers.
    """

    task: str
    implementor: Hashable
    problem: Hashable
    replicate: Hashable
    value: float
    cell: Hashable | None = None


@dataclass(frozen=True, slots=True)
class StratifiedEstimate:
    """Equal-task benchmark estimate and its conditional repeat interval."""

    mean: float | None
    se: float | None
    df: float | None
    ci95_low: float | None
    ci95_high: float | None
    tasks: int
    replicates: int
    cells: int
    balanced: bool


class ScoreArm(StrEnum):
    """Closed set of benchmark arms with distinct rerun estimands."""

    AGENT = "agent"
    EXISTING = "existing"
    NO_LIBRARY = "no-library"


@dataclass(frozen=True, slots=True)
class ScoreReplicate:
    """Canonical provenance for one arm's independent execution."""

    arm: ScoreArm
    task: str
    condition: str
    replicate: tuple[str, str, int | None]
    source_design_run: str | None
    source_evaluation_run: str
    attempt: int | None
    inferential: bool


@dataclass(frozen=True, slots=True)
class ScoreObservation:
    """One finite persisted score with logical and execution identities."""

    arm: ScoreArm
    condition: str
    task: str
    implementor: Hashable
    problem: Hashable
    provenance: ScoreReplicate
    value: float
    execution: Hashable


def normalize_score_replicate(
    *,
    task: str,
    library_kind: str,
    condition: str,
    attempt: int | None,
    source_design_run: str | None,
    source_evaluation_run: str,
) -> ScoreReplicate:
    """Normalize one persisted score row's condition-specific replicate.

    All score readers use this one identity rule. Controls share a logical
    condition by arm kind and condition name, but their replicate identity also
    includes the evaluation run that executed them. An authored artifact's
    canonical source design run identifies its condition and generated-library
    replicate; missing provenance uses the sole fallback, a unique opaque trial
    identity, and is never inferential.
    """
    arm = ScoreArm(library_kind)
    if arm is ScoreArm.AGENT:
        if source_design_run:
            condition_identity = f"agent:{source_design_run}"
            replicate_identity = condition_identity
            inferential = attempt is not None
        else:
            condition_identity = f"agent:unknown:{source_evaluation_run}"
            replicate_identity = f"{condition_identity}:{condition}"
            inferential = False
    else:
        condition_identity = f"{library_kind}:{condition}"
        replicate_identity = f"{condition_identity}:{source_evaluation_run}"
        inferential = attempt is not None
    return ScoreReplicate(
        arm=arm,
        task=task,
        condition=condition_identity,
        replicate=(replicate_identity, task, attempt),
        source_design_run=source_design_run,
        source_evaluation_run=source_evaluation_run,
        attempt=attempt,
        inferential=inferential,
    )


def score_observation(
    *,
    provenance: ScoreReplicate,
    task: str,
    implementor: Hashable,
    problem: Hashable,
    value: float,
    execution: Hashable,
) -> ScoreObservation:
    """Build a typed observation from provenance normalized at the load boundary."""
    return ScoreObservation(
        arm=provenance.arm,
        condition=provenance.condition,
        task=task,
        implementor=implementor,
        problem=problem,
        provenance=provenance,
        value=value,
        execution=execution,
    )


def score_interval_available(replicates: Sequence[ScoreReplicate]) -> bool:
    """Whether every task supplies one inferential score condition."""
    conditions: defaultdict[str, set[str]] = defaultdict(set)
    for replicate in replicates:
        conditions[replicate.task].add(replicate.condition)
    return (
        bool(replicates)
        and all(replicate.inferential for replicate in replicates)
        and all(len(task_conditions) == 1 for task_conditions in conditions.values())
    )


def estimate_score(
    observations: Sequence[ScoreObservation],
    *,
    coverage_complete: bool = False,
) -> StratifiedEstimate:
    """Estimate one benchmark arm under its fixed-benchmark rerun design."""
    if not observations:
        return stratified_mean(())
    if any(not isfinite(observation.value) for observation in observations):
        raise ValueError("score observations must be finite")
    arms = {observation.arm for observation in observations}
    executions = [observation.execution for observation in observations]
    eligible = (
        coverage_complete
        and len(executions) == len(set(executions))
        and all(
            observation.arm is observation.provenance.arm
            and observation.task == observation.provenance.task
            and observation.condition == observation.provenance.condition
            for observation in observations
        )
    )
    if len(arms) != 1:
        eligible = False
    arm = next(iter(arms)) if len(arms) == 1 else ScoreArm.AGENT
    if arm is ScoreArm.AGENT:
        replicates = tuple(observation.provenance for observation in observations)
        estimate = stratified_mean(
            tuple(
                StratifiedObservation(
                    task=observation.task,
                    implementor=observation.implementor,
                    problem=observation.problem,
                    replicate=observation.provenance.replicate,
                    value=observation.value,
                    cell=observation.execution,
                )
                for observation in observations
            ),
            coverage_complete=eligible,
        )
        if not score_interval_available(replicates):
            return replace(estimate, se=None, df=None, ci95_low=None, ci95_high=None)
        return estimate
    return _control_score(observations, eligible=eligible)


def _control_score(
    observations: Sequence[ScoreObservation], *, eligible: bool
) -> StratifiedEstimate:
    """Estimate independent attempts within fixed task/implementor strata."""
    execution_groups: defaultdict[
        tuple[str, str, Hashable, str, int | None], list[ScoreObservation]
    ] = defaultdict(list)
    conditions: defaultdict[str, set[str]] = defaultdict(set)
    for observation in observations:
        provenance = observation.provenance
        conditions[observation.task].add(observation.condition)
        execution_groups[
            (
                observation.condition,
                observation.task,
                observation.implementor,
                provenance.source_evaluation_run,
                provenance.attempt,
            )
        ].append(observation)
    eligible = eligible and all(len(value) == 1 for value in conditions.values())

    strata: defaultdict[
        tuple[str, Hashable], list[tuple[float, frozenset[Hashable]]]
    ] = defaultdict(list)
    for (_, task, implementor, _, attempt), rows in execution_groups.items():
        if attempt is None or any(not row.provenance.inferential for row in rows):
            eligible = False
        problems = frozenset(row.problem for row in rows)
        if len(problems) != len(rows):
            eligible = False
        strata[(task, implementor)].append(
            (fsum(row.value for row in rows) / len(rows), problems)
        )
    tasks = {task for task, _ in strata}
    implementors = {implementor for _, implementor in strata}
    keys = set(strata)
    eligible = eligible and all(
        (task, implementor) in keys for task in tasks for implementor in implementors
    )
    attempt_counts = {len(attempts) for attempts in strata.values()}
    eligible = (
        eligible and len(attempt_counts) == 1 and min(attempt_counts, default=0) >= 2
    )
    eligible = eligible and all(
        len({panel for _, panel in attempts}) == 1 for attempts in strata.values()
    )
    eligible = eligible and all(
        len(
            {
                attempts[0][1]
                for (stratum_task, _), attempts in strata.items()
                if stratum_task == task
            }
        )
        == 1
        for task in tasks
    )
    stratum_means = {
        key: tuple(mean for mean, _ in attempts) for key, attempts in strata.items()
    }
    task_means = [
        fsum(
            fsum(means) / len(means)
            for (stratum_task, _), means in stratum_means.items()
            if stratum_task == task
        )
        / sum(1 for stratum_task, _ in stratum_means if stratum_task == task)
        for task in tasks
    ]
    mean = fsum(task_means) / len(task_means)
    base = StratifiedEstimate(
        mean=mean,
        se=None,
        df=None,
        ci95_low=None,
        ci95_high=None,
        tasks=len(tasks),
        replicates=len(execution_groups),
        cells=len(observations),
        balanced=eligible,
    )
    if not eligible:
        return base
    contributions = tuple(
        variance(means) / (len(tasks) ** 2 * len(implementors) ** 2 * len(means))
        for means in stratum_means.values()
    )
    total = fsum(contributions)
    if total == 0:
        return replace(base, se=0.0)
    denominator = fsum(
        contribution**2 / (len(means) - 1)
        for contribution, means in zip(
            contributions, stratum_means.values(), strict=True
        )
    )
    df = total**2 / denominator
    se = sqrt(total)
    margin = _student_t_critical(df) * se
    return replace(base, se=se, df=df, ci95_low=mean - margin, ci95_high=mean + margin)


@dataclass(frozen=True, slots=True)
class ClusteredEstimate:
    """One measure's equal-weight mean and task-clustered standard error."""

    mean: float | None
    se: float | None
    n: int
    clusters: int


def stratified_mean(
    observations: Sequence[StratifiedObservation],
    *,
    coverage_complete: bool = False,
) -> StratifiedEstimate:
    """Return the paper's fixed-task rerun estimate.

    Problem-by-implementor cells first form complete library-run scores. Runs
    then average within tasks and tasks receive equal benchmark weight.
    The interval uses only within-task rerun variation. It is unavailable
    unless the caller establishes declared coverage, every task has the same
    number of runs, every run for a task has the same downstream panel,
    every task uses the same implementors, and every task contributes at
    least two runs.
    """
    grouped: defaultdict[str, defaultdict[Hashable, list[StratifiedObservation]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for observation in observations:
        grouped[observation.task][observation.replicate].append(observation)
    if not grouped:
        return StratifiedEstimate(
            mean=None,
            se=None,
            df=None,
            ci95_low=None,
            ci95_high=None,
            tasks=0,
            replicates=0,
            cells=0,
            balanced=False,
        )
    replicate_means = {
        task: tuple(
            fsum(observation.value for observation in cells) / len(cells)
            for cells in replicates.values()
        )
        for task, replicates in grouped.items()
    }
    task_means = tuple(fsum(means) / len(means) for means in replicate_means.values())
    mean = fsum(task_means) / len(task_means)
    tasks = len(grouped)
    replicates = sum(len(means) for means in replicate_means.values())
    cells = sum(
        len(run_cells)
        for task_runs in grouped.values()
        for run_cells in task_runs.values()
    )
    replicate_counts = {len(task_runs) for task_runs in grouped.values()}
    implementor_panels = {
        frozenset(observation.implementor for observation in run_cells)
        for task_runs in grouped.values()
        for run_cells in task_runs.values()
    }
    panels_match = all(
        len(
            {
                frozenset(
                    (observation.implementor, observation.problem)
                    for observation in run_cells
                )
                for run_cells in task_runs.values()
            }
        )
        == 1
        and all(
            len(run_cells)
            == len(
                {
                    (
                        observation.implementor,
                        observation.cell
                        if observation.cell is not None
                        else observation.problem,
                    )
                    for observation in run_cells
                }
            )
            and len(
                {
                    frozenset(
                        observation.problem
                        for observation in run_cells
                        if observation.implementor == implementor
                    )
                    for implementor in {
                        observation.implementor for observation in run_cells
                    }
                }
            )
            == 1
            for run_cells in task_runs.values()
        )
        for task_runs in grouped.values()
    )
    balanced = (
        len(replicate_counts) == 1 and len(implementor_panels) == 1 and panels_match
    )
    if (
        not coverage_complete
        or not balanced
        or any(len(means) < 2 for means in replicate_means.values())
    ):
        return StratifiedEstimate(
            mean=mean,
            se=None,
            df=None,
            ci95_low=None,
            ci95_high=None,
            tasks=tasks,
            replicates=replicates,
            cells=cells,
            balanced=balanced,
        )
    contributions = tuple(
        variance(means) / (tasks**2 * len(means)) for means in replicate_means.values()
    )
    variance_estimate = fsum(contributions)
    if variance_estimate == 0:
        return StratifiedEstimate(
            mean=mean,
            se=0.0,
            df=None,
            ci95_low=None,
            ci95_high=None,
            tasks=tasks,
            replicates=replicates,
            cells=cells,
            balanced=balanced,
        )
    denominator = fsum(
        contribution**2 / (len(means) - 1)
        for contribution, means in zip(
            contributions, replicate_means.values(), strict=True
        )
    )
    if denominator == 0:
        raise ValueError("positive stratified variance has no degrees of freedom")
    degrees_of_freedom = variance_estimate**2 / denominator
    standard_error = sqrt(variance_estimate)
    critical_value = _student_t_critical(degrees_of_freedom)
    margin = critical_value * standard_error
    return StratifiedEstimate(
        mean=mean,
        se=standard_error,
        df=degrees_of_freedom,
        ci95_low=mean - margin,
        ci95_high=mean + margin,
        tasks=tasks,
        replicates=replicates,
        cells=cells,
        balanced=balanced,
    )


def _student_t_critical(degrees_of_freedom: float) -> float:
    """Return the two-sided 95% Student-t critical value without a dependency."""
    target = 0.975
    low = 0.0
    high = NormalDist().inv_cdf(target)
    while _student_t_cdf(high, degrees_of_freedom) < target:
        high *= 2
    for _ in range(80):
        middle = (low + high) / 2
        if _student_t_cdf(middle, degrees_of_freedom) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _student_t_cdf(value: float, degrees_of_freedom: float) -> float:
    """Return Student-t CDF through its regularized incomplete-beta form."""
    ratio = degrees_of_freedom / (degrees_of_freedom + value**2)
    tail = _regularized_beta(ratio, degrees_of_freedom / 2, 0.5) / 2
    return 1 - tail if value >= 0 else tail


def _regularized_beta(value: float, alpha: float, beta: float) -> float:
    """Return I_x(alpha, beta) with a stable continued fraction."""
    if value <= 0:
        return 0.0
    if value >= 1:
        return 1.0
    front = exp(
        lgamma(alpha + beta)
        - lgamma(alpha)
        - lgamma(beta)
        + alpha * log(value)
        + beta * log(1 - value)
    )
    threshold = (alpha + 1) / (alpha + beta + 2)
    if value < threshold:
        return front * _beta_fraction(alpha, beta, value) / alpha
    return 1 - front * _beta_fraction(beta, alpha, 1 - value) / beta


def _beta_fraction(alpha: float, beta: float, value: float) -> float:
    """Evaluate the incomplete-beta continued fraction (Numerical Recipes)."""
    tiny = 1e-300
    current = 1.0
    denominator = 1 - (alpha + beta) * value / (alpha + 1)
    denominator = tiny if abs(denominator) < tiny else denominator
    denominator = 1 / denominator
    fraction = denominator
    for iteration in range(1, 201):
        even = 2 * iteration
        coefficient = (
            iteration
            * (beta - iteration)
            * value
            / ((alpha + even - 1) * (alpha + even))
        )
        denominator = 1 + coefficient * denominator
        denominator = tiny if abs(denominator) < tiny else denominator
        current = 1 + coefficient / current
        current = tiny if abs(current) < tiny else current
        denominator = 1 / denominator
        fraction *= denominator * current
        coefficient = (
            -(alpha + iteration)
            * (alpha + beta + iteration)
            * value
            / ((alpha + even) * (alpha + even + 1))
        )
        denominator = 1 + coefficient * denominator
        denominator = tiny if abs(denominator) < tiny else denominator
        current = 1 + coefficient / current
        current = tiny if abs(current) < tiny else current
        denominator = 1 / denominator
        change = denominator * current
        fraction *= change
        if abs(change - 1) < 3e-14:
            return fraction
    raise ValueError("incomplete beta continued fraction did not converge")


def clustered_mean(
    observations: Sequence[tuple[Hashable, float]],
) -> ClusteredEstimate:
    """Equal-weight mean and cluster-robust SE of per-cell values keyed by cluster."""
    n = len(observations)
    if n == 0:
        return ClusteredEstimate(mean=None, se=None, n=0, clusters=0)
    mean = fsum(value for _, value in observations) / n
    grouped: defaultdict[Hashable, list[float]] = defaultdict(list)
    for key, value in observations:
        grouped[key].append(value)
    clusters = len(grouped)
    if clusters < 2:
        return ClusteredEstimate(mean=mean, se=None, n=n, clusters=clusters)
    totals = [fsum(value - mean for value in values) for values in grouped.values()]
    variance = clusters / (clusters - 1) * fsum(total * total for total in totals)
    return ClusteredEstimate(mean=mean, se=sqrt(variance) / n, n=n, clusters=clusters)
