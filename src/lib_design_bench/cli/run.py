"""`ldb run` boundary for one experiment: a design run and its cells."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from typing import Any

import structlog
import typer
from harbor.models.trial.config import EnvironmentConfig

from lib_design_bench.cli.common import DEFAULT_CONCURRENCY
from lib_design_bench.cli.common import OverridesArgument
from lib_design_bench.cli.common import RunAgentEnvOption
from lib_design_bench.cli.common import RunAgentKwargsOption
from lib_design_bench.cli.common import RunAgentVersionOption
from lib_design_bench.cli.common import RunAllowedAgentHostsOption
from lib_design_bench.cli.common import RunDebugOption
from lib_design_bench.cli.common import RunForceOption
from lib_design_bench.cli.common import RunImplementorsOption
from lib_design_bench.cli.common import RunNameOption
from lib_design_bench.cli.common import RunReasoningOption
from lib_design_bench.cli.common import agent_environment
from lib_design_bench.cli.common import allowed_agent_hosts
from lib_design_bench.cli.common import config_overrides
from lib_design_bench.cli.common import echo_result_json
from lib_design_bench.cli.common import live_agent
from lib_design_bench.cli.common import load_tasks
from lib_design_bench.cli.common import new_run_directory
from lib_design_bench.cli.common import normalized_cli_filters
from lib_design_bench.cli.common import override_document
from lib_design_bench.cli.common import replacement_environment
from lib_design_bench.cli.common import sandbox_environment
from lib_design_bench.cli.common import select_problems
from lib_design_bench.cli.display import display_experiment_result
from lib_design_bench.common import ReasoningLevel
from lib_design_bench.logging import route_console_to_stderr
from lib_design_bench.models.experiment import ConfigOverride
from lib_design_bench.models.experiment import ExperimentConfig
from lib_design_bench.models.experiment import load_experiment_config
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Request
from lib_design_bench.models.manifest import DesignLaunch
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import ExperimentRecord
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import ExperimentResult
from lib_design_bench.models.reports import RunReport
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.replay import settle
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.pipeline.run import lease
from lib_design_bench.reports.rebuild import experiment_view
from lib_design_bench.runs.outcomes import classify
from lib_design_bench.runs.outcomes import classify_run
from lib_design_bench.runs.outcomes import launches_in
from lib_design_bench.runs.plan import grow_evaluation_run
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import EVALUATION_RESULTS_DIR_NAME
from lib_design_bench.runs.store import MANIFEST_FILE_NAME
from lib_design_bench.runs.store import Run

logger = structlog.get_logger(__name__)


DEFAULT_OUTPUT_DIR = Path("runs/agent_attempts")
"""Where a new experiment lands when `--output` is not given."""


ExperimentTargetArgument = Annotated[
    Path,
    typer.Argument(
        help=(
            "Experiment configuration file describing both phases, or an "
            "existing experiment directory to continue."
        ),
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
    ),
]


ExperimentAgentOption = Annotated[
    str | None,
    typer.Option(
        "--agent", "-a", help="Harbor agent name or import path for the design agent."
    ),
]


ExperimentModelOption = Annotated[
    str | None,
    typer.Option("--model", "-m", help="Model identifier passed to the design agent."),
]


ExperimentOutputDirOption = Annotated[
    Path | None,
    typer.Option(
        "--output",
        "-o",
        help=f"Root output directory for new experiments; defaults to {DEFAULT_OUTPUT_DIR}.",
        readable=True,
        writable=True,
        file_okay=False,
    ),
]


ExperimentConcurrencyOption = Annotated[
    int | None,
    typer.Option(
        "--n-concurrent",
        "-n",
        help=(
            "Maximum concurrent trials in each phase; defaults to "
            f"{DEFAULT_CONCURRENCY} for a new experiment and to the persisted "
            "value when continuing one."
        ),
    ),
]


ExperimentTasksOption = Annotated[
    list[str] | None,
    typer.Option(
        "--task",
        help=(
            "For a new experiment, persist only the named task(s). For an "
            "existing experiment, run only those tasks' unfinished trials "
            "and cells. Repeat to select several."
        ),
    ),
]


ExperimentProblemsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--problem",
        help=(
            "Run only the unfinished cells of the named Evaluation Phase problem. "
            "Repeat to select several; omit to run every problem the "
            "experiment contains."
        ),
    ),
]


ExperimentHoldDesignRerunsOption = Annotated[
    bool,
    typer.Option(
        "--hold-design-reruns",
        help=(
            "When continuing an experiment, do not relaunch design trials in "
            "the rerun class (agent errors, usage limits, missing results). "
            "Their tasks keep their cells held back and are reported at "
            "the end; every other task proceeds into the Evaluation Phase."
        ),
    ),
]


ExperimentDesignOnlyOption = Annotated[
    bool,
    typer.Option(
        "--design-only",
        help=(
            "Stop once the design run is finalized, planning no cells and "
            "writing a partial experiment result. Continue the same directory "
            "without this flag to plan and run every cell."
        ),
    ),
]


ExperimentJsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Print the experiment result document instead of the score tables.",
    ),
]


@dataclass(frozen=True)
class _Selection:
    """Which of an experiment's unfinished trials this invocation runs.

    A persisted request defines an experiment's task scope. On a config
    argument, `--task` first narrows that scope before this selection runs;
    on a directory argument every filter chooses execution scope. An empty axis
    admits every launch, and a named value has to exist somewhere in the
    experiment. The Design Phase has no problems and no implementors, so only
    the task filter narrows it.
    """

    tasks: tuple[str, ...]
    problems: tuple[str, ...]
    implementors: tuple[str, ...]

    def admits(self, launch: TrialLaunch) -> bool:
        """Report whether one planned launch passes every filter that applies."""
        if self.tasks and launch.task.name not in self.tasks:
            return False
        if not isinstance(launch, EvaluationLaunch):
            return True
        if self.problems and launch.problem.name not in self.problems:
            return False
        return not self.implementors or launch.arm.label in self.implementors

    def selected(
        self, launches: tuple[TrialLaunch, ...], *, phase: str
    ) -> tuple[TrialLaunch, ...]:
        """Return the unfinished launches this invocation runs, and log them."""
        chosen: list[TrialLaunch] = []
        deferred: list[str] = []
        for launch in launches:
            if self.admits(launch):
                chosen.append(launch)
            else:
                deferred.append(launch.trial_name)
        logger.info(
            "Selected the unfinished trials this invocation runs.",
            phase=phase,
            unfinished_count=len(launches),
            selected_count=len(chosen),
            deferred=deferred,
        )
        return tuple(chosen)


@dataclass(frozen=True)
class _Experiment:
    """One experiment's resolved location, design request, and settings.

    Both argument forms produce this, so creating an experiment and
    continuing one differ only in where these values came from. A config-form
    `--task` selection is already represented by `design_request` and
    config; problem and implementor selection plus `design_only`
    belong only to this invocation.
    """

    dir: Path
    design_request: AuthorJob
    record: ExperimentRecord
    selection: _Selection
    design_only: bool
    hold_design_reruns: bool
    n_concurrent: int
    evaluation_environment: EnvironmentConfig

    @property
    def evaluation_dir(self) -> Path:
        """Return the one evaluation run inside this experiment."""
        return self.dir / EVALUATION_RESULTS_DIR_NAME


def run_experiment_command(
    target: ExperimentTargetArgument,
    overrides: OverridesArgument = None,
    agent: ExperimentAgentOption = None,
    model: ExperimentModelOption = None,
    reasoning: RunReasoningOption = None,
    agent_version: RunAgentVersionOption = None,
    agent_kwargs: RunAgentKwargsOption = None,
    agent_env: RunAgentEnvOption = None,
    allowed_hosts: RunAllowedAgentHostsOption = None,
    output_dir: ExperimentOutputDirOption = None,
    run_name: RunNameOption = None,
    n_concurrent: ExperimentConcurrencyOption = None,
    tasks: ExperimentTasksOption = None,
    problems: ExperimentProblemsOption = None,
    eval_agents: RunImplementorsOption = None,
    design_only: ExperimentDesignOnlyOption = False,
    hold_design_reruns: ExperimentHoldDesignRerunsOption = False,
    force: RunForceOption = False,
    debug: RunDebugOption = False,
    output_json: ExperimentJsonOption = False,
) -> None:
    """Author one library per task, then evaluate every implementor cell.

    A configuration file starts a new experiment, with `KEY=VALUE` overrides
    applied to it; the design agent comes from `--agent` and `--model`, and
    `--agent-env` and `--allow-agent-host` reach every agent. The
    experiment's own directory continues it from the manifest it persisted,
    rerunning the slots without usable evidence and replaying the verifier for
    the slots that only failed grading; only `environment.*` overrides apply
    there. A config-form `--task` defines the persisted experiment scope; on a
    directory argument it narrows unfinished work within that scope.
    `--problem` and `--eval-agent` choose this invocation's work, and
    `--design-only` stops it after the design run.
    """
    if output_json:
        route_console_to_stderr()
    filters = _Selection(
        tasks=normalized_cli_filters(tasks, "--task"),
        problems=normalized_cli_filters(problems, "--problem"),
        implementors=normalized_cli_filters(eval_agents, "--eval-agent"),
    )
    if target.is_dir():
        experiment = _continue_experiment(
            target,
            request_shaping={
                "--output": output_dir,
                "--name": run_name,
                "--agent": agent,
                "--model": model,
                "--reasoning": reasoning,
                "--agent-version": agent_version,
                "--agent-kwargs": agent_kwargs,
                "--agent-env": agent_env,
                "--allow-agent-host": allowed_hosts,
            },
            environment=override_document(
                overrides, allowed=frozenset({"environment"})
            ).get("environment"),
            filters=filters,
            n_concurrent=n_concurrent,
            design_only=design_only,
            hold_design_reruns=hold_design_reruns,
        )
    else:
        if hold_design_reruns:
            raise typer.BadParameter(
                "`--hold-design-reruns` only applies when continuing an "
                "experiment directory; a new experiment has no design trials "
                "to hold back.",
                param_hint="--hold-design-reruns",
            )
        experiment = _new_experiment(
            target,
            overrides=config_overrides(overrides),
            agent=agent,
            model=model,
            reasoning=reasoning,
            agent_version=agent_version,
            agent_kwargs=agent_kwargs,
            agent_env=agent_environment(agent_env),
            allowed_hosts=allowed_agent_hosts(allowed_hosts),
            filters=filters,
            output_dir=output_dir,
            run_name=run_name,
            n_concurrent=n_concurrent,
            design_only=design_only,
        )
    _execute(
        experiment,
        force=force,
        debug_build_contexts=debug,
        output_json=output_json,
    )


def _new_experiment(
    config: Path,
    *,
    overrides: tuple[ConfigOverride, ...],
    agent: str | None,
    model: str | None,
    reasoning: ReasoningLevel | None,
    agent_version: str | None,
    agent_kwargs: list[str] | None,
    agent_env: dict[str, str],
    allowed_hosts: tuple[str, ...],
    filters: _Selection,
    output_dir: Path | None,
    run_name: str | None,
    n_concurrent: int | None,
    design_only: bool,
) -> _Experiment:
    """Resolve one configuration file into a new, empty experiment directory."""
    if agent is None or model is None:
        raise typer.BadParameter("Missing option '--agent' and '--model'.")
    try:
        experiment_config = load_experiment_config(
            config,
            overrides=overrides,
            agent_env=agent_env,
            allowed_hosts=allowed_hosts,
        )
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="CONFIG") from error
    configured_tasks = load_tasks(
        list(experiment_config.tasks), tasks_root=experiment_config.tasks_root
    )
    _require_known(
        filters.tasks,
        {task.name for task in configured_tasks},
        option="--task",
        label="task",
    )
    tasks = tuple(
        task
        for task in configured_tasks
        if not filters.tasks or task.name in filters.tasks
    )
    experiment_config = experiment_config.model_copy(
        update={"tasks": tuple(task.name for task in tasks)}
    )
    selection = _validated_selection(filters, tasks, experiment_config)
    design_agent = live_agent(
        agent,
        model,
        reasoning=reasoning,
        agent_version=agent_version,
        agent_kwargs=agent_kwargs,
        env=agent_env,
        allowed_hosts=allowed_hosts,
    )
    concurrency = DEFAULT_CONCURRENCY if n_concurrent is None else n_concurrent
    design_request = AuthorJob(
        tasks=tasks,
        agent=design_agent,
        prompt_template=experiment_config.design.prompt,
        attempts=tuple(range(1, experiment_config.design.attempts + 1)),
        n_concurrent=concurrency,
        environment=sandbox_environment(
            experiment_config.environment, experiment_config.design.sandbox
        ),
    )
    record = ExperimentRecord(
        overrides=tuple(override.text for override in overrides),
        config=experiment_config,
        design_agent=design_agent,
    )
    try:
        run_dir = new_run_directory(
            output_dir or DEFAULT_OUTPUT_DIR, run_name, kind="run"
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    run_dir.mkdir(parents=True)
    logger.info(
        "Starting experiment.",
        experiment_dir=run_dir.as_posix(),
        config=config.as_posix(),
        design_agent=agent,
        design_model=model,
        tasks=[task.name for task in design_request.tasks],
        design_attempt_count=experiment_config.design.attempts,
        implementors=list(experiment_config.evaluation.agents),
        overrides=list(record.overrides),
        n_concurrent=concurrency,
        design_only=design_only,
    )
    return _Experiment(
        dir=run_dir,
        design_request=design_request,
        record=record,
        selection=selection,
        design_only=design_only,
        hold_design_reruns=False,
        n_concurrent=concurrency,
        evaluation_environment=sandbox_environment(
            experiment_config.environment, experiment_config.evaluation.sandbox
        ),
    )


def _validated_selection(
    filters: _Selection, tasks: tuple[Task, ...], config: ExperimentConfig
) -> _Selection:
    """Check every filter against what this experiment contains, and log them.

    A filter chooses execution scope, so a value naming nothing in the whole
    experiment is a typo rather than an empty invocation: it is rejected here,
    against everything the experiment holds and not against what is still
    unfinished.
    """
    _require_known(
        filters.tasks,
        {task.name for task in tasks},
        option="--task",
        label="task",
    )
    _require_known(
        filters.problems,
        {problem.name for problem in select_problems(tasks, ())},
        option="--problem",
        label="problem",
    )
    _require_known(
        filters.implementors,
        set(config.evaluation.agents),
        option="--eval-agent",
        label="implementor",
    )
    logger.info(
        "Applying the selection this invocation runs.",
        tasks=list(filters.tasks),
        problems=list(filters.problems),
        implementors=list(filters.implementors),
    )
    return filters


def _require_known(
    selected: tuple[str, ...], available: set[str], *, option: str, label: str
) -> None:
    """Reject filter values this experiment has no trial for."""
    unknown = tuple(name for name in selected if name not in available)
    if unknown:
        raise typer.BadParameter(
            f"Unknown {label}(s): {', '.join(unknown)}. This experiment contains: "
            f"{', '.join(sorted(available))}.",
            param_hint=option,
        )


def _continue_experiment(
    run_dir: Path,
    *,
    request_shaping: dict[str, object],
    environment: dict[str, Any] | None,
    filters: _Selection,
    n_concurrent: int | None,
    design_only: bool,
    hold_design_reruns: bool,
) -> _Experiment:
    """Reopen a persisted experiment, keeping every request it was planned for.

    The manifests are the authority: the design request, its task scope,
    and the published config are read back exactly as they are on disk, so the
    options that would rebuild a different request are rejected. Selection,
    concurrency, and `environment.*` overrides remain available within that
    persisted scope.
    """
    given = sorted(
        option for option, value in request_shaping.items() if value is not None
    )
    if given:
        raise typer.BadParameter(
            f"{', '.join(given)} shape a new experiment and cannot be combined "
            "with an experiment directory.",
            param_hint=given[0],
        )
    if not Run.exists(run_dir):
        raise typer.BadParameter(
            f"Not a run directory: {run_dir}", param_hint="EXPERIMENT"
        )
    persisted = Run.open(run_dir)
    record = persisted.manifest.experiment
    request = persisted.request()
    if record is None or not isinstance(request, AuthorJob):
        raise typer.BadParameter(
            f"Run directory is not an experiment: {run_dir}", param_hint="EXPERIMENT"
        )
    execution_environment = persisted_evaluation_environment(run_dir, record)
    design_request = request
    if environment is not None:
        execution_environment = sandbox_environment(
            replacement_environment(execution_environment.type, environment),
            record.config.evaluation.sandbox,
        )
        design_request = request.model_copy(
            update={
                "environment": sandbox_environment(
                    replacement_environment(request.environment.type, environment),
                    record.config.design.sandbox,
                )
            }
        )
    logger.info(
        "Continuing experiment.",
        experiment_dir=run_dir.as_posix(),
        implementors=list(record.config.evaluation.agents),
        tasks=[task.name for task in request.tasks],
        design_only=design_only,
        hold_design_reruns=hold_design_reruns,
        evaluation_environment=execution_environment.type,
    )
    return _Experiment(
        dir=run_dir,
        design_request=design_request,
        record=record,
        selection=_validated_selection(filters, request.tasks, record.config),
        design_only=design_only,
        hold_design_reruns=hold_design_reruns,
        n_concurrent=request.n_concurrent if n_concurrent is None else n_concurrent,
        evaluation_environment=execution_environment,
    )


def persisted_evaluation_environment(
    run_dir: Path, record: ExperimentRecord
) -> EnvironmentConfig:
    """Return the environment an experiment's evaluation run was planned with.

    An experiment without one yet takes its config's evaluation sandbox.
    """
    evaluation_dir = run_dir / EVALUATION_RESULTS_DIR_NAME
    if not Run.exists(evaluation_dir):
        return sandbox_environment(
            record.config.environment, record.config.evaluation.sandbox
        )
    # Author artifacts may need recreating before the full evaluation request
    # can be loaded; read only its execution settings here.
    return EnvironmentConfig.model_validate(
        json.loads((evaluation_dir / MANIFEST_FILE_NAME).read_text())["request"][
            "environment"
        ]
    )


def plan_evaluation_cells(
    design_run: Run, *, n_concurrent: int, environment: EnvironmentConfig
) -> tuple[TrialLaunch, ...]:
    """Grow an experiment's evaluation run to every cell; return the unfinished.

    Nothing launches here, so a cell whose library is not available yet is
    still planned and reported.
    """
    record = design_run.manifest.experiment
    request = design_run.request()
    if record is None or not isinstance(request, AuthorJob):
        raise ValueError(f"Not an experiment: {design_run.dir}")
    evaluation_dir = design_run.dir / EVALUATION_RESULTS_DIR_NAME
    return _planned(
        grow_evaluation_run(
            design_run=design_run,
            evaluation_dir=evaluation_dir,
            config=record.config,
            problems=select_problems(request.tasks, ()),
            n_concurrent=n_concurrent,
            environment=environment,
        ),
        evaluation_dir,
        record=None,
    )


def _execute(
    experiment: _Experiment,
    *,
    force: bool,
    debug_build_contexts: bool,
    output_json: bool,
) -> None:
    """Drive both phases under their leases, always writing the result document.

    Design work happens first because every cell mounts one of its
    libraries. Every cell is then planned, so the result reports all of them.
    A task whose author trials all settled runs its cells; one still in the
    rerun class holds only its own cells back, so an experiment makes whatever
    progress its finished tasks allow. A design-only invocation plans the
    cells but runs none.

    Author trials this invocation selected and still could not settle are the
    work it was asked for and failed to do, so they are reported at ERROR and
    end it nonzero once both runs have written what they have.
    """
    design_report: RunReport | None = None
    evaluation_report: RunReport | None = None
    outstanding: tuple[str, ...] = ()
    with asyncio.Runner() as runner, lease(experiment.dir, force=force):
        try:
            design_report = _finish_design(
                experiment,
                debug_build_contexts=debug_build_contexts,
                runner=runner,
            )
            design_run = Run.open(experiment.dir)
            outstanding = tuple(
                f"{launch.trial_name}: {classify(design_run.slot(launch)).reason}"
                for launch in launches_in(design_run, "rerun")
                if experiment.selection.admits(launch)
            )
            # A design-only invocation runs no cell, so its environment
            # overrides leave the evaluation run's alone.
            cells = plan_evaluation_cells(
                design_run,
                n_concurrent=experiment.n_concurrent,
                environment=persisted_evaluation_environment(
                    experiment.dir, experiment.record
                )
                if experiment.design_only
                else experiment.evaluation_environment,
            )
            if experiment.design_only:
                logger.info(
                    "Stopping after the design run; its cells are planned but "
                    "not run. Continue this directory without `--design-only` "
                    "to run them.",
                    experiment_dir=experiment.dir.as_posix(),
                    implementors=list(experiment.record.config.evaluation.agents),
                )
            else:
                evaluation_report = _finish_evaluation(
                    experiment,
                    cells,
                    force=force,
                    debug_build_contexts=debug_build_contexts,
                    runner=runner,
                )
        finally:
            if Run.exists(experiment.dir):
                result = _experiment_result(
                    experiment,
                    design_report=design_report,
                    evaluation_report=evaluation_report,
                )
    if outstanding:
        logger.error(
            "Design attempts produced no usable result, so the tasks they "
            "author have no cells to run. Rerun them with "
            f"`ldb run {experiment.dir.as_posix()}`.",
            experiment_dir=experiment.dir.as_posix(),
            rerun_slots=list(outstanding),
        )
        raise typer.Exit(code=1)
    logger.info(
        "Experiment finished.",
        experiment_dir=experiment.dir.as_posix(),
        complete=result.complete,
        design_only=result.design_only,
        outcome_counts=result.outcome_counts,
    )
    if output_json:
        echo_result_json(experiment.dir)
    else:
        display_experiment_result(result)


def _finish_design(
    experiment: _Experiment,
    *,
    debug_build_contexts: bool,
    runner: asyncio.Runner,
) -> RunReport:
    """Author the selected libraries, then regrade what only failed grading."""
    persisted_launches = _planned(
        experiment.design_request, experiment.dir, record=experiment.record
    )
    launches = experiment.selection.selected(
        tuple(
            launch.model_copy(
                update={"environment": experiment.design_request.environment}
            )
            if isinstance(launch, DesignLaunch)
            else launch
            for launch in persisted_launches
        ),
        phase="design",
    )
    if experiment.hold_design_reruns and Run.exists(experiment.dir):
        design_run = Run.open(experiment.dir)
        held = tuple(
            launch
            for launch in launches
            if classify(design_run.slot(launch)).outcome == "rerun"
        )
        launches = tuple(launch for launch in launches if launch not in held)
        logger.info(
            "Holding back design trials in the rerun class.",
            experiment_dir=experiment.dir.as_posix(),
            held_trials=[launch.trial_name for launch in held],
        )
    logger.info(
        "Authoring libraries.",
        experiment_dir=experiment.dir.as_posix(),
        author_trial_count=len(launches),
    )
    return settle(
        launches,
        experiment.dir,
        n_concurrent=experiment.n_concurrent,
        environment=experiment.design_request.environment,
        debug_build_contexts=debug_build_contexts,
        runner=runner,
    )


def _finish_evaluation(
    experiment: _Experiment,
    unfinished: tuple[TrialLaunch, ...],
    *,
    force: bool,
    debug_build_contexts: bool,
    runner: asyncio.Runner,
) -> RunReport:
    """Run, regrade, and refresh the selected unfinished cells."""
    evaluation_dir = experiment.evaluation_dir
    cells = experiment.selection.selected(unfinished, phase="evaluation")
    logger.info(
        "Evaluating authored libraries.",
        evaluation_dir=evaluation_dir.as_posix(),
        cell_count=len(cells),
        implementor_count=len(experiment.record.config.evaluation.agents),
    )
    with lease(evaluation_dir, force=force):
        return settle(
            cells,
            evaluation_dir,
            n_concurrent=experiment.n_concurrent,
            environment=experiment.evaluation_environment,
            debug_build_contexts=debug_build_contexts,
            runner=runner,
        )


def _planned(
    request: Request, run_dir: Path, *, record: ExperimentRecord | None
) -> tuple[TrialLaunch, ...]:
    """Plan or reopen one run, reporting a rejected plan as a bad argument.

    A persisted request that no longer expands, such as one hand-edited into
    duplicate trial names, is a fault in the directory the user named.
    """
    try:
        return plan(request, run_dir, experiment=record)
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="EXPERIMENT") from error


def _experiment_result(
    experiment: _Experiment,
    *,
    design_report: RunReport | None,
    evaluation_report: RunReport | None,
) -> ExperimentResult:
    """Build the result document from whatever both runs have persisted.

    An interrupted experiment reaches this without one or both reports, so the
    persisted slots are finalized here and every cell that never ran keeps the
    rerun class its empty slot earns.
    """
    design_run = Run.open(experiment.dir)
    design = finalize(experiment.dir) if design_report is None else design_report
    outcomes = classify_run(design_run)
    evaluation_run = design_run.evaluation_child()
    evaluation = None
    if evaluation_run is not None:
        outcomes |= classify_run(evaluation_run)
        evaluation = (
            evaluation_run,
            finalize(evaluation_run.dir)
            if evaluation_report is None
            else evaluation_report,
        )
    result = experiment_view(
        design_run,
        design,
        evaluation,
        outcomes=outcomes,
        design_only=experiment.design_only,
    )
    if result is None:
        raise ValueError(f"Not an experiment: {experiment.dir}")
    return result
