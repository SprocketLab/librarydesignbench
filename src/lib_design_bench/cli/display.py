"""Console tables for runs, experiments, and static references."""

from __future__ import annotations

from collections import Counter
from collections import defaultdict
from collections.abc import Hashable
from collections.abc import Sequence

from rich.table import Table

from lib_design_bench.logging import get_rich_console
from lib_design_bench.metrics.uncertainty import clustered_mean
from lib_design_bench.metrics.uncertainty import estimate_score
from lib_design_bench.metrics.uncertainty import normalize_score_replicate
from lib_design_bench.metrics.uncertainty import score_observation
from lib_design_bench.models import RunReport
from lib_design_bench.models.reports import EvaluationRow
from lib_design_bench.models.reports import ExperimentResult
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.pipeline.references import StaticReferenceRow
from lib_design_bench.reports.results import capped_simplicity
from lib_design_bench.reports.results import trial_score_estimate

CI_CAPTION = "approximate fixed-benchmark 95% rerun CI"
"""What every benchmark-score interval in this module reports."""


def display_evaluation_scores(report: RunReport) -> None:
    """Print library conditions and a final aggregate for an Evaluation Phase run.

    A condition row's `N` is its number of attempt labels. The final `all` row
    pools completed cells directly, so its `N` is the number of measured cells.
    """
    console = get_rich_console()
    table = Table(
        title="Evaluation Phase library conditions",
        header_style="bold cyan",
        caption=CI_CAPTION,
    )
    table.add_column("Condition", style="bold", no_wrap=True)
    table.add_column("N", justify="right", no_wrap=True, min_width=1)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("Pass rate", justify="right", no_wrap=True)
    table.add_column("Simplicity", justify="right", no_wrap=True)
    table.add_column("Cost", justify="right", no_wrap=True)
    table.add_column("Incomplete", justify="right", no_wrap=True)
    grouped = defaultdict(list)
    for trial in report.attempts:
        grouped[(trial.library, trial.library_kind)].append(trial)
    name_counts = Counter(name for name, _kind in grouped)
    for (name, kind), trials in grouped.items():
        table.add_row(
            name if name_counts[name] == 1 else f"{name} ({kind})",
            str(len({trial.attempt for trial in trials})),
            *_trial_measures(trials, source_evaluation_run=report.run_id),
        )
    table.add_section()
    table.add_row(
        "all",
        str(len(report.attempts)),
        *_trial_measures(report.attempts, source_evaluation_run=report.run_id),
        style="bold",
    )
    console.print(table)


def _trial_measures(
    trials: Sequence[TrialReport], *, source_evaluation_run: str
) -> tuple[str, str, str, str, str]:
    """Render score, pass rate, simplicity, cost, and incomplete count of trials."""
    keyed = [
        ((trial.task, trial.design_attempt), trial)
        for trial in trials
        if trial.incomplete_reason is None
    ]
    return (
        _trial_score(trials, source_evaluation_run=source_evaluation_run),
        _estimate([(key, trial.pass_rate or 0.0) for key, trial in keyed], ""),
        _estimate(
            [(key, capped_simplicity(trial.simplicity_ratios)) for key, trial in keyed],
            "",
        ),
        f"${sum(trial.usage.cost_usd or 0.0 for trial in trials):,.3f}",
        str(sum(trial.incomplete_reason is not None for trial in trials)),
    )


def display_experiment_result(result: ExperimentResult) -> None:
    """Print one implementor table and one task table for an experiment.

    Both tables summarize the same measured evaluation cells from two sides, so
    a reader sees which implementor benefited and which task carried the
    result without a row per library attempt. `N` counts the cells a row
    averages and `Incomplete` counts every cell excluded from those measures,
    including terminal cells whose required measurement is unavailable.

    A design-only result is the outcome of authoring alone, so it shows what
    its design run produced instead.
    """
    if result.design_only:
        _display_design_rows(result)
        return
    console = get_rich_console()
    console.print(_implementor_table(result))
    console.print(_task_table(result))


def _implementor_table(result: ExperimentResult) -> Table:
    """Return one row per implementor, forcing incomplete score cells to zero."""
    grouped: defaultdict[str, list[EvaluationRow]] = defaultdict(list)
    incomplete: Counter[str] = Counter()
    for row in result.evaluation:
        grouped[row.implementor].append(row)
        if row.outcome != "finished" or row.incomplete_reason is not None:
            incomplete[row.implementor] += 1
    table = Table(
        title="Implementors",
        header_style="bold cyan",
        caption=_caption(result),
    )
    table.add_column("Implementor", style="bold", no_wrap=True)
    table.add_column("N", justify="right", no_wrap=True)
    for name in ("Score", "Pass Rate", "Simplicity", "Eval $"):
        table.add_column(name, justify="right")
    table.add_column("Incomplete", justify="right", no_wrap=True)
    coverage_complete = _score_coverage_complete(result)
    for implementor in result.implementors:
        cells = grouped[implementor]
        table.add_row(
            implementor,
            str(len(cells)),
            *_cell_measures(cells, coverage_complete=coverage_complete),
            str(incomplete[implementor]),
        )
    return table


def _task_table(result: ExperimentResult) -> Table:
    """Return one row per task plus an equal-task aggregate row."""
    grouped: defaultdict[str, list[EvaluationRow]] = defaultdict(list)
    incomplete: Counter[str] = Counter()
    for row in result.evaluation:
        grouped[row.task].append(row)
        if row.outcome != "finished" or row.incomplete_reason is not None:
            incomplete[row.task] += 1
    design_costs: defaultdict[str, list[float]] = defaultdict(list)
    for design_row in result.design:
        if design_row.cost_usd is not None:
            design_costs[design_row.task].append(design_row.cost_usd)
    table = Table(
        title="Tasks",
        header_style="bold cyan",
        caption=_caption(result),
    )
    table.add_column("Task", style="bold", no_wrap=True)
    table.add_column("N", justify="right", no_wrap=True)
    for name in ("Score", "Pass Rate", "Simplicity", "Design $", "Eval $"):
        table.add_column(name, justify="right")
    table.add_column("Incomplete", justify="right", no_wrap=True)
    tasks = sorted(
        {row.task for row in result.evaluation}
        | {design_row.task for design_row in result.design}
    )
    coverage_complete = _score_coverage_complete(result)
    for task in tasks:
        cells = grouped[task]
        measures = _cell_measures(cells, coverage_complete=coverage_complete)
        table.add_row(
            task,
            str(len(cells)),
            *measures[:3],
            _total_dollars(design_costs[task]),
            measures[3],
            str(incomplete[task]),
        )
    pooled = list(result.evaluation)
    measures = _cell_measures(pooled, coverage_complete=coverage_complete)
    table.add_section()
    table.add_row(
        "all",
        str(len(pooled)),
        *measures[:3],
        _total_dollars([cost for costs in design_costs.values() for cost in costs]),
        measures[3],
        str(sum(incomplete.values())),
        style="bold",
    )
    return table


def _score_coverage_complete(result: ExperimentResult) -> bool:
    """Whether every declared library run has a terminal fixed-panel evaluation."""
    if not result.evaluation or any(
        row.outcome != "finished" for row in result.evaluation
    ):
        return False
    if result.design and any(row.outcome != "finished" for row in result.design):
        return False
    expected_runs = {
        (row.task, row.design_attempt)
        for row in result.design
        if row.outcome == "finished"
    }
    observed_implementors: defaultdict[tuple[str, int], set[str]] = defaultdict(set)
    for row in result.evaluation:
        observed_implementors[(row.task, row.design_attempt)].add(row.implementor)
    expected_implementors = set(result.implementors)
    return not expected_runs or (
        set(observed_implementors) == expected_runs
        and all(
            observed_implementors[run] == expected_implementors for run in expected_runs
        )
    )


def _cell_measures(
    rows: Sequence[EvaluationRow], *, coverage_complete: bool
) -> tuple[str, str, str, str]:
    """Render score, pass rate, simplicity, and evaluation cost over given cells.

    Simplicity is the capped component, never the raw ratio the rows persist.
    A cell that recorded no cost is no cost observation rather than a zero one,
    so it reads as `n/a` instead of claiming the run was free.
    """
    measured = [
        row
        for row in rows
        if row.outcome == "finished" and row.incomplete_reason is None
    ]
    return (
        _evaluation_score(rows, coverage_complete=coverage_complete),
        _estimate(
            [
                ((row.task, row.design_attempt), row.pass_rate or 0.0)
                for row in measured
            ],
            "",
        ),
        _estimate(
            [
                (
                    (row.task, row.design_attempt),
                    capped_simplicity(row.simplicity_ratios),
                )
                for row in measured
            ],
            "",
        ),
        _estimate(
            [
                ((row.task, row.design_attempt), row.cost_usd)
                for row in measured
                if row.cost_usd is not None
            ],
            "$",
        ),
    )


def _estimate(observations: Sequence[tuple[Hashable, float]], prefix: str) -> str:
    """Render one library-clustered estimate as `mean ± se` at three decimals."""
    estimate = clustered_mean(observations)
    if estimate.mean is None:
        return "n/a"
    if estimate.se is None:
        return f"{prefix}{estimate.mean:,.3f}"
    return f"{prefix}{estimate.mean:,.3f} ± {estimate.se:,.3f}"


def _trial_score(trials: Sequence[TrialReport], *, source_evaluation_run: str) -> str:
    """Render one typed score estimate without deriving uncertainty in the view."""
    estimate = trial_score_estimate(trials, source_evaluation_run=source_evaluation_run)
    return _render_score_estimate(
        estimate.mean, estimate.se, estimate.ci95_low, estimate.ci95_high
    )


def _evaluation_score(rows: Sequence[EvaluationRow], *, coverage_complete: bool) -> str:
    """Render finalized experiment cells, zeroing only settled incompletes."""
    finalized = tuple(row for row in rows if row.outcome == "finished")
    replicates = tuple(
        normalize_score_replicate(
            task=row.task,
            library_kind="agent",
            condition="experiment",
            attempt=row.design_attempt,
            # An experiment result belongs to one persisted design run, so its
            # own document is sufficient provenance for these rows.
            source_design_run="experiment-result",
            source_evaluation_run="experiment-result",
        )
        for row in finalized
    )
    estimate = estimate_score(
        tuple(
            score_observation(
                provenance=replicate,
                task=row.task,
                implementor=row.implementor,
                problem=row.problem,
                value=(row.score if row.incomplete_reason is None else 0.0),
                execution=(
                    "experiment-result",
                    row.task,
                    row.problem,
                    row.implementor,
                    row.design_attempt,
                    row.evaluation_attempt,
                ),
            )
            for row, replicate in zip(finalized, replicates, strict=True)
        ),
        coverage_complete=coverage_complete,
    )
    return _render_score_estimate(
        estimate.mean, estimate.se, estimate.ci95_low, estimate.ci95_high
    )


def _render_score_estimate(
    mean: float | None,
    se: float | None,
    ci95_low: float | None,
    ci95_high: float | None,
) -> str:
    if mean is None:
        return "n/a"
    if se == 0.0 and (ci95_low is None or ci95_high is None):
        return f"{mean:,.3f} (no rerun variation observed)"
    if ci95_low is None or ci95_high is None:
        return f"{mean:,.3f}"
    return f"{mean:,.3f} [{ci95_low:,.3f}, {ci95_high:,.3f}]"


def _total_dollars(costs: Sequence[float]) -> str:
    """Render a plain sum of dollars, which no standard error describes."""
    return f"${sum(costs):,.3f}" if costs else "n/a"


def _caption(result: ExperimentResult) -> str:
    """Describe the estimator and the evidence both experiment tables pool."""
    cells = list(result.evaluation)
    libraries = {(row.task, row.design_attempt) for row in cells}
    tasks = {row.task for row in cells}
    return (
        f"{CI_CAPTION}; {len(cells)} cells, {len(libraries)} libraries, "
        f"{len(tasks)} equally weighted tasks; finalized incomplete cells score zero"
    )


def _display_design_rows(result: ExperimentResult) -> None:
    """Print one row per author attempt of every task."""
    console = get_rich_console()
    table = Table(title="Design run", header_style="bold cyan")
    table.add_column("Task", style="bold", no_wrap=True)
    table.add_column("Attempt", justify="right", no_wrap=True)
    table.add_column("Reward", justify="right", no_wrap=True)
    table.add_column("Outcome", no_wrap=True)
    for row in result.design:
        table.add_row(
            row.task,
            str(row.design_attempt),
            "n/a" if row.reward is None else f"{row.reward:.3f}",
            row.outcome,
        )
    console.print(table)


def display_static_references(rows: tuple[StaticReferenceRow, ...]) -> None:
    """Print each refreshed problem reference and its measured metrics."""
    table = Table(title="Static references", header_style="bold cyan")
    table.add_column("Task", style="bold")
    table.add_column("Problem")
    table.add_column("Reference")
    table.add_column("Statements", justify="right")
    table.add_column("SLOC", justify="right")
    table.add_column("Cognitive", justify="right")
    table.add_column("Cyclomatic", justify="right")
    table.add_column("Halstead", justify="right")
    table.add_column("Parse tokens", justify="right")
    for row in rows:
        metrics = row.metrics
        table.add_row(
            row.task,
            row.problem,
            row.library,
            _render_number(metrics.stmts),
            _render_number(metrics.sloc),
            _render_number(metrics.cog_complex),
            _render_number(metrics.cyc_complex),
            _render_number(metrics.halstead_volume),
            _render_number(metrics.parse_tokens),
        )
    get_rich_console().print(table)


def _render_number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,}"
