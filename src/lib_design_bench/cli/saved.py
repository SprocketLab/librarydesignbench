"""Commands that rework a saved run in place: resume and recalculate."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import structlog
import typer

from lib_design_bench.cli.common import DEFAULT_CONCURRENCY
from lib_design_bench.cli.common import OverridesArgument
from lib_design_bench.cli.common import RunConcurrencyOption
from lib_design_bench.cli.common import RunForceOption
from lib_design_bench.cli.common import RunJsonOption
from lib_design_bench.cli.common import default_sandbox_environment
from lib_design_bench.cli.common import echo_result_json
from lib_design_bench.cli.common import environment_config
from lib_design_bench.cli.common import override_document
from lib_design_bench.cli.common import replacement_environment
from lib_design_bench.cli.display import display_evaluation_scores
from lib_design_bench.cli.display import display_experiment_result
from lib_design_bench.cli.run import persisted_evaluation_environment
from lib_design_bench.cli.run import plan_evaluation_cells
from lib_design_bench.logging import route_console_to_stderr
from lib_design_bench.metrics.costs import CostRates
from lib_design_bench.metrics.costs import ImplementorPricing
from lib_design_bench.metrics.costs import Pricing
from lib_design_bench.metrics.costs import load_model_pricing
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.pipeline.replay import recalculate
from lib_design_bench.pipeline.replay import remeasure
from lib_design_bench.pipeline.replay import settle
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.pipeline.run import lease_subtree
from lib_design_bench.reports.rebuild import persisted_report
from lib_design_bench.runs.plan import persist_request
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import Run

logger = structlog.get_logger(__name__)


def resume_command(
    run_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Typed run directory to continue in place.",
        ),
    ],
    overrides: OverridesArgument = None,
    force: RunForceOption = False,
    n_concurrent: Annotated[
        int | None,
        typer.Option("--n-concurrent", "-n", help="Override concurrency for this run."),
    ] = None,
    output_json: RunJsonOption = False,
) -> None:
    """Resolve saved slots by rerunning, regrading, or reanalyzing evidence.

    Slots in the rerun class launch again; re-verify slots are regraded from
    saved artifacts; reanalyze slots refresh retained static measurements;
    finished slots remain untouched. An experiment plans every evaluation cell
    it lacks without running any. Finalization then rebuilds the
    reader-facing reports without extra agent work.

    `environment.*` overrides replace the run's persisted execution
    environment, keeping its sandbox size, so unfinished slots run on another
    provider and later resumes keep that choice unless told otherwise.

    Rerunning and replaying share one event loop: a remote environment keeps
    locks and clients bound to the loop that created them, so a second loop
    fails every trial it is given.
    """
    if output_json:
        route_console_to_stderr()
    environment = override_document(overrides, allowed=frozenset({"environment"})).get(
        "environment"
    )
    # Rejected overrides must fail before the lease, which logs any error
    # raised inside it as a failed run.
    replacement = None
    if environment is not None:
        saved = Run.open(run_dir).request().environment
        replacement = environment_config(
            replacement_environment(saved.type, environment),
            cpus=saved.override_cpus,
            memory_mb=saved.override_memory_mb,
            storage_mb=saved.override_storage_mb,
        )
    with asyncio.Runner() as runner, lease_subtree(run_dir, force=force):
        persisted = Run.open(run_dir)
        request = persisted.request()
        concurrency = request.n_concurrent if n_concurrent is None else n_concurrent
        if replacement is not None:
            request = request.model_copy(update={"environment": replacement})
            logger.info(
                "Switching the run's execution environment.",
                run_dir=run_dir.as_posix(),
                previous_environment=persisted.request().environment.type,
                environment=request.environment.type,
            )
            persist_request(
                run_dir, persisted.manifest.model_copy(update={"request": request})
            )
        settle(
            plan(request, run_dir),
            run_dir,
            n_concurrent=concurrency,
            environment=request.environment,
            debug_build_contexts=False,
            runner=runner,
        )
        record = persisted.manifest.experiment
        if record is not None:
            plan_evaluation_cells(
                Run.open(run_dir),
                n_concurrent=concurrency,
                environment=persisted_evaluation_environment(run_dir, record),
            )
        child = Run.open(run_dir).evaluation_child()
        if child is not None:
            remeasure(
                child,
                n_concurrent=concurrency,
                environment=child.request().environment,
                runner=runner,
            )
            finalize(child.dir)
    persisted = Run.open(run_dir)
    report = persisted_report(persisted)
    if output_json:
        echo_result_json(persisted.result_owner().dir)
    elif isinstance(persisted.request(), AuthorJob):
        typer.echo(
            f"Design Run resumed: {len(report.attempts)} trial(s) under {run_dir}"
        )
    else:
        display_evaluation_scores(report)


def _pricing(
    input_cost: float | None,
    output_cost: float | None,
    cache_input_cost: float | None,
    implementor: str | None,
    pricing_config: Path | None,
) -> Pricing | None:
    """Normalize the two mutually exclusive CLI pricing policies.

    The rate options price one implementor at one rate set; a pricing config
    prices every model it names at once, and needs no implementor because a
    model already says which cells it ran.
    """
    if pricing_config is not None:
        conflicting = [
            option
            for option, value in (
                ("--input-cost", input_cost),
                ("--output-cost", output_cost),
                ("--cache-input-cost", cache_input_cost),
                ("--implementor", implementor),
            )
            if value is not None
        ]
        if conflicting:
            raise typer.BadParameter(
                f"--pricing-config already prices every model it names, "
                f"so it cannot be combined with {', '.join(conflicting)}"
            )
        try:
            return load_model_pricing(pricing_config)
        except ValueError as error:
            raise typer.BadParameter(
                str(error), param_hint="--pricing-config"
            ) from error
    selected = None if implementor is None else implementor.strip()
    if implementor is not None and not selected:
        raise typer.BadParameter("--implementor cannot be empty")
    if input_cost is None and output_cost is None and cache_input_cost is None:
        if selected is not None:
            raise typer.BadParameter("--implementor requires cost rates")
        return None
    if input_cost is None or output_cost is None or cache_input_cost is None:
        raise typer.BadParameter(
            "--input-cost, --output-cost, and --cache-input-cost must be supplied together"
        )
    try:
        rates = CostRates(
            input_per_million=input_cost,
            output_per_million=output_cost,
            cache_input_per_million=cache_input_cost,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    return ImplementorPricing(rates=rates, implementor=selected)


def recalculate_command(
    path: Annotated[
        Path,
        typer.Argument(
            help="Saved experiment, design run, or evaluation run to recalculate.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    overrides: OverridesArgument = None,
    n_concurrent: RunConcurrencyOption = DEFAULT_CONCURRENCY,
    input_cost: Annotated[
        float | None,
        typer.Option(help="Input-token price in USD per million tokens."),
    ] = None,
    output_cost: Annotated[
        float | None,
        typer.Option(help="Output-token price in USD per million tokens."),
    ] = None,
    cache_input_cost: Annotated[
        float | None,
        typer.Option(help="Cached-input price in USD per million tokens."),
    ] = None,
    implementor: Annotated[
        str | None,
        typer.Option(help="Reprice only cells for this implementor."),
    ] = None,
    pricing_config: Annotated[
        Path | None,
        typer.Option(
            help="YAML file pricing several models at once, by model name.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Remeasure saved workspaces in their task verifiers and rewrite every report.

    An experiment first plans every cell its evaluation run lacks, so the
    result lists all of them. Takes `environment.*` overrides for the replays
    that remeasure; sandbox sizes are the Harbor task defaults.
    """
    environment = default_sandbox_environment(overrides)
    pricing = _pricing(
        input_cost, output_cost, cache_input_cost, implementor, pricing_config
    )
    try:
        owner = Run.open(path).result_owner()
        record = owner.manifest.experiment
        if record is not None:
            plan_evaluation_cells(
                owner,
                n_concurrent=owner.request().n_concurrent,
                environment=persisted_evaluation_environment(owner.dir, record),
            )
        rebuilt = recalculate(
            path, pricing, n_concurrent=n_concurrent, environment=environment
        )
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    experiment = next(
        (run.experiment for run in rebuilt if run.experiment is not None), None
    )
    if experiment is not None:
        display_experiment_result(experiment)
        return
    for run in rebuilt:
        if run.report.run_type == "evaluation":
            display_evaluation_scores(run.report)
