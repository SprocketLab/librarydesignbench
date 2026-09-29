"""The one path that rewrites a saved run's derived documents.

Finalization, static refresh, the verifier replay a `--update` commits, and an
explicit recalculation all end here. Every document a run derives from its
slots is rebuilt through the same builders, never patched in place: one trial
report per slot, then the single `ldb-result.json` at the result owner, which
is the experiment root for an experiment's evaluation child. The stale result
is removed before the trial reports change, so a reader never finds one that
disagrees with the trial evidence beneath it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import structlog
from harbor.models.trial.result import TrialResult

from lib_design_bench.metrics.costs import CostRates
from lib_design_bench.metrics.costs import persisted_cost_rates
from lib_design_bench.metrics.costs import standardize_usage_cost
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.reports import RESULT_FILE_NAME
from lib_design_bench.models.reports import ExperimentResult
from lib_design_bench.models.reports import OutcomeClass
from lib_design_bench.models.reports import RunReport
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.models.reports import UsageReport
from lib_design_bench.reports.results import build_experiment_result
from lib_design_bench.reports.results import build_result
from lib_design_bench.reports.results import run_report
from lib_design_bench.reports.trials import build_trial_report
from lib_design_bench.runs.outcomes import classify_run
from lib_design_bench.runs.store import LDB_CONFIG_FILE_NAME
from lib_design_bench.runs.store import TRIAL_REPORT_FILE_NAME
from lib_design_bench.runs.store import OutputDocument
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import RunOutputConfig
from lib_design_bench.runs.store import final_run_documents
from lib_design_bench.runs.store import load_sandbox_usage
from lib_design_bench.runs.store import sandbox_ledger_path
from lib_design_bench.runs.store import write_run_output_documents

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RebuiltReports:
    """One run's regenerated report and the experiment view it completes."""

    report: RunReport
    experiment: ExperimentResult | None


def rebuild_reports(
    run: Run,
    results: Mapping[str, TrialResult | None],
    extra_documents: tuple[OutputDocument, ...],
    *,
    repricing: Mapping[str, CostRates] | None,
) -> RebuiltReports:
    """Rewrite every document one run derives, from the results it is given.

    `results` holds the Harbor result of each planned trial by name, so a
    caller that recomputed a reward reports the trial with what it recomputed
    rather than with what the slot still holds on disk. `extra_documents` are
    that caller's own slot documents, written in the same transaction as the
    reports so a reader never sees a reward the reports disagree with.

    `repricing` names the trials one explicit pricing policy reprices and the
    rates each is repriced at. Under a policy, a trial it omits keeps the cost
    fields its report already held rather than being re-derived; without a
    policy, every trial is re-derived at whatever rates it was last priced at.
    """
    launches = run.launches()
    trial_reports = {
        launch.trial_name: build_trial_report(
            launch,
            results.get(launch.trial_name),
            load_sandbox_usage(
                run.slot(launch).dir,
                (
                    "unknown"
                    if launch.environment.type is None
                    else launch.environment.type.value
                ),
                sandbox_ledger_path(run.dir, launch.trial_name),
            ),
        )
        for launch in launches
    }
    previous = persisted_report(run)
    previous_trials = (
        {} if previous is None else {t.trial_name: t for t in previous.attempts}
    )
    for name, trial in trial_reports.items():
        previous_trial = previous_trials.get(name)
        prior = None if previous_trial is None else previous_trial.usage
        same_execution = (
            previous_trial is not None and previous_trial.trial_uri == trial.trial_uri
        )
        selected = None if repricing is None else repricing.get(name)
        if repricing is not None and selected is None:
            if same_execution and prior is not None:
                trial_reports[name] = trial.model_copy(
                    update={"usage": _preserve_costs(trial.usage, prior)}
                )
            continue
        # A slot whose fresh usage lost its token and cost evidence retains
        # the last known price, even without an explicit repricing policy.
        has_fresh_evidence = any(
            value is not None
            for value in (
                trial.usage.input_tokens,
                trial.usage.uncached_input_tokens,
                trial.usage.cache_input_tokens,
                trial.usage.output_tokens,
                trial.usage.cost_usd,
            )
        )
        rates = selected if selected is not None else persisted_cost_rates(prior)
        if rates is None:
            if same_execution and prior is not None and not has_fresh_evidence:
                trial_reports[name] = trial.model_copy(
                    update={"usage": _preserve_costs(trial.usage, prior)}
                )
            continue
        source = (
            prior
            if same_execution and prior is not None and not has_fresh_evidence
            else trial.usage
        )
        trial_reports[name] = trial.model_copy(
            update={"usage": standardize_usage_cost(source, rates)}
        )
    report = _run_report(run, trial_reports)
    run.unpublish_result()
    write_run_output_documents(
        (
            *extra_documents,
            *final_run_documents(
                run,
                report,
                tuple(result for result in results.values() if result is not None),
            ),
        )
    )
    # An experiment's evaluation run publishes through its design run, so
    # rebuilding either one republishes the pair from both runs' evidence.
    owner = run.result_owner()
    owner_report = report if owner.dir == run.dir else persisted_report(owner)
    if owner_report is None:
        raise ValueError(f"Missing finalized design result: {owner.dir}")
    child = owner.evaluation_child()
    child_report = (
        None
        if child is None
        else report
        if child.dir == run.dir
        else persisted_report(child)
    )
    evaluation = (
        None if child is None or child_report is None else (child, child_report)
    )
    outcomes = classify_run(owner)
    if evaluation is not None:
        outcomes |= classify_run(evaluation[0])
    write_run_output_documents(
        (
            OutputDocument(
                owner.dir / RESULT_FILE_NAME,
                build_result(
                    owner, owner_report, evaluation=evaluation, outcomes=outcomes
                ).to_json(),
            ),
        )
    )
    # A design run's view waits for its evaluation run's own rebuild.
    experiment = (
        None
        if owner.dir == run.dir and child is not None
        else experiment_view(
            owner,
            owner_report,
            evaluation,
            outcomes=outcomes,
            design_only=evaluation is None,
        )
    )
    logger.debug(
        "Rebuilt run documents.",
        run_dir=run.dir.as_posix(),
        trial_count=len(report.attempts),
        experiment=experiment is not None,
    )
    return RebuiltReports(report=report, experiment=experiment)


def persisted_report(run: Run) -> RunReport | None:
    """Read a run's report back from its slots' persisted trial reports.

    A slot without a trial report is projected from its Harbor result. A run
    that has published nothing yet has no report.
    """
    launches = run.launches()
    if not (run.dir / RESULT_FILE_NAME).is_file() and not any(
        (run.slot(launch).dir / TRIAL_REPORT_FILE_NAME).is_file() for launch in launches
    ):
        return None
    reports = {}
    for launch in launches:
        slot = run.slot(launch)
        reports[launch.trial_name] = slot.trial_report() or build_trial_report(
            launch, slot.result()
        )
    return _run_report(run, reports)


def experiment_view(
    design_run: Run,
    design_report: RunReport,
    evaluation: tuple[Run, RunReport] | None,
    *,
    outcomes: Mapping[str, OutcomeClass],
    design_only: bool,
) -> ExperimentResult | None:
    """Build the console's experiment view, or nothing outside an experiment.

    `outcomes` classifies every slot of both runs by trial name.
    """
    record = design_run.manifest.experiment
    if record is None:
        return None
    return build_experiment_result(
        design_report=design_report,
        evaluation_report=None if evaluation is None else evaluation[1],
        evaluation_launches=()
        if evaluation is None
        else tuple(
            launch
            for launch in evaluation[0].launches()
            if isinstance(launch, EvaluationLaunch)
        ),
        outcomes=outcomes,
        repo_commit=design_run.manifest.repo_commit,
        tasks_hash=design_run.manifest.tasks_hash,
        design_agent=record.design_agent,
        implementors=tuple(record.config.evaluation.agents),
        design_only=design_only,
    )


def _run_report(run: Run, trial_reports: Mapping[str, TrialReport]) -> RunReport:
    return run_report(
        run.manifest,
        run.launches(),
        trial_reports,
        execution=RunOutputConfig.load(run.dir / LDB_CONFIG_FILE_NAME).summary(),
    ).model_copy(update={"run_id": run.dir.name})


def _preserve_costs(current: UsageReport, previous: UsageReport) -> UsageReport:
    """Keep an excluded trial's effective, source, and pricing cost fields."""
    return current.model_copy(
        update={
            "cost_usd": previous.cost_usd,
            "reported_cost_usd": previous.reported_cost_usd,
            "standardized_cost_usd": previous.standardized_cost_usd,
            "input_cost_per_million": previous.input_cost_per_million,
            "output_cost_per_million": previous.output_cost_per_million,
            "cache_input_cost_per_million": previous.cache_input_cost_per_million,
        }
    )
