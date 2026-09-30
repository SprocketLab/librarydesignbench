"""`ldb eval`: one Evaluation Run of one implementor agent under one condition.

Every subcommand takes the same agent, evaluation settings, `KEY=VALUE`
overrides, and flags; the subcommand alone picks the library condition the
implementor gets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import structlog
import typer
from harbor.models.trial.config import EnvironmentConfig

from lib_design_bench.cli.common import DEFAULT_CONCURRENCY
from lib_design_bench.cli.common import OverridesArgument
from lib_design_bench.cli.common import RunAgentEnvOption
from lib_design_bench.cli.common import RunAgentKwargsOption
from lib_design_bench.cli.common import RunAgentVersionOption
from lib_design_bench.cli.common import RunAllowedAgentHostsOption
from lib_design_bench.cli.common import RunConcurrencyOption
from lib_design_bench.cli.common import RunDebugOption
from lib_design_bench.cli.common import RunForceOption
from lib_design_bench.cli.common import RunJsonOption
from lib_design_bench.cli.common import RunNameOption
from lib_design_bench.cli.common import RunOutputDirOption
from lib_design_bench.cli.common import RunProblemsOption
from lib_design_bench.cli.common import RunReasoningOption
from lib_design_bench.cli.common import RunTasksOption
from lib_design_bench.cli.common import agent_environment
from lib_design_bench.cli.common import allowed_agent_hosts
from lib_design_bench.cli.common import display_evaluation
from lib_design_bench.cli.common import live_agent
from lib_design_bench.cli.common import load_tasks
from lib_design_bench.cli.common import new_run_directory
from lib_design_bench.cli.common import normalized_cli_filters
from lib_design_bench.cli.common import override_document
from lib_design_bench.cli.common import override_environment
from lib_design_bench.cli.common import sandbox_environment
from lib_design_bench.cli.common import select_task_problem_pairs
from lib_design_bench.cli.common import select_task_problem_pairs_from_tasks
from lib_design_bench.common import ReasoningLevel
from lib_design_bench.common import get_repo_root
from lib_design_bench.logging import route_console_to_stderr
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.conditions import LibraryCondition
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.experiment import EvaluationSettings
from lib_design_bench.models.experiment import Sandbox
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import arm_label
from lib_design_bench.models.manifest import DesignLaunch
from lib_design_bench.models.task import ExistingLibraryEntry
from lib_design_bench.models.task import Problem
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.run import execute
from lib_design_bench.runs.plan import UNSETTLED_DESIGN_REASON
from lib_design_bench.runs.plan import evaluation_job
from lib_design_bench.runs.store import Run

logger = structlog.get_logger(__name__)


DEFAULT_PROMPT = get_repo_root() / "configs" / "prompts" / "library_use_inst.md"
"""The official Evaluation Phase prompt template."""


eval_app = typer.Typer(
    help="Evaluate one implementor agent under one library condition.",
    no_args_is_help=True,
)


AgentOption = Annotated[
    str,
    typer.Option(
        "--agent",
        "-a",
        help="Harbor agent name or `module:Class` import path for the implementor.",
    ),
]


ModelOption = Annotated[
    str,
    typer.Option("--model", "-m", help="Model identifier passed to the implementor."),
]


AttemptsOption = Annotated[
    int,
    typer.Option("--attempts", min=1, help="Implementor attempts per cell."),
]


PromptOption = Annotated[
    Path,
    typer.Option(
        "--prompt",
        help="Jinja prompt template containing `{{instruction}}`.",
        show_default="the official evaluation prompt",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
]


CpusOption = Annotated[int, typer.Option("--cpus", min=1, help="Sandbox CPU count.")]


MemoryMbOption = Annotated[
    int, typer.Option("--memory-mb", min=1, help="Sandbox memory in MiB.")
]


StorageMbOption = Annotated[
    int, typer.Option("--storage-mb", min=1, help="Sandbox storage in MiB.")
]


@dataclass(frozen=True)
class _Setup:
    """The implementor, settings, and provider every subcommand resolves alike."""

    settings: EvaluationSettings
    environment: EnvironmentConfig
    tasks_root: Path | None


@eval_app.command(name="design")
def eval_design(
    design_dir: Annotated[
        Path,
        typer.Argument(
            help="Design Run whose settled author attempts are the libraries.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    agent: AgentOption,
    model: ModelOption,
    overrides: OverridesArgument = None,
    reasoning: RunReasoningOption = None,
    agent_version: RunAgentVersionOption = None,
    agent_kwargs: RunAgentKwargsOption = None,
    agent_env: RunAgentEnvOption = None,
    allowed_hosts: RunAllowedAgentHostsOption = None,
    attempts: AttemptsOption = 1,
    prompt: PromptOption = DEFAULT_PROMPT,
    cpus: CpusOption = 2,
    memory_mb: MemoryMbOption = 4096,
    storage_mb: StorageMbOption = 10240,
    output_dir: RunOutputDirOption = Path("runs"),
    run_name: RunNameOption = None,
    n_concurrent: RunConcurrencyOption = DEFAULT_CONCURRENCY,
    tasks: RunTasksOption = None,
    problems: RunProblemsOption = None,
    force: RunForceOption = False,
    debug: RunDebugOption = False,
    output_json: RunJsonOption = False,
) -> None:
    """Evaluate the libraries a Design Run's author attempts produced.

    Takes `environment.*` and `tasks_root` overrides.
    """
    setup = _setup(
        agent=agent,
        model=model,
        reasoning=reasoning,
        agent_version=agent_version,
        agent_kwargs=agent_kwargs,
        agent_env=agent_env,
        allowed_hosts=allowed_hosts,
        attempts=attempts,
        prompt=prompt,
        sandbox=Sandbox(cpus=cpus, memory_mb=memory_mb, storage_mb=storage_mb),
        overrides=overrides,
    )
    try:
        design_run = Run.open(design_dir)
    except (FileNotFoundError, ValueError) as error:
        raise typer.BadParameter(
            f"DESIGN_DIR is not a valid Design Run: {error}"
        ) from error
    if not isinstance(design_run.request(), AuthorJob):
        raise typer.BadParameter("DESIGN_DIR must be a Design Run.")
    authored = {
        launch.task.name: launch.task
        for launch in design_run.launches()
        if isinstance(launch, DesignLaunch)
    }
    available = (
        tuple(authored.values())
        if setup.tasks_root is None
        else tuple(
            task
            for task in load_tasks((), tasks_root=setup.tasks_root)
            if task.name in authored
        )
    )
    selection = select_task_problem_pairs_from_tasks(
        available,
        normalized_cli_filters(tasks, "--task"),
        normalized_cli_filters(problems, "--problem"),
    )
    request = evaluation_job(
        design_run=design_run,
        settings=setup.settings,
        problems=selection.problems,
        n_concurrent=n_concurrent,
        environment=setup.environment,
    )
    if all(
        isinstance(arm.condition, AuthoredArtifact)
        and arm.condition.incomplete_reason == UNSETTLED_DESIGN_REASON
        for arm in request.arms
    ):
        raise typer.BadParameter(
            "DESIGN_DIR has no selected task whose author attempts all settled."
        )
    _evaluate(
        request,
        output_dir=output_dir,
        name=run_name,
        force=force,
        debug=debug,
        output_json=output_json,
    )


@eval_app.command(name="no-library")
def eval_no_library(
    agent: AgentOption,
    model: ModelOption,
    overrides: OverridesArgument = None,
    reasoning: RunReasoningOption = None,
    agent_version: RunAgentVersionOption = None,
    agent_kwargs: RunAgentKwargsOption = None,
    agent_env: RunAgentEnvOption = None,
    allowed_hosts: RunAllowedAgentHostsOption = None,
    attempts: AttemptsOption = 1,
    prompt: PromptOption = DEFAULT_PROMPT,
    cpus: CpusOption = 2,
    memory_mb: MemoryMbOption = 4096,
    storage_mb: StorageMbOption = 10240,
    output_dir: RunOutputDirOption = Path("runs"),
    run_name: RunNameOption = None,
    n_concurrent: RunConcurrencyOption = DEFAULT_CONCURRENCY,
    tasks: RunTasksOption = None,
    problems: RunProblemsOption = None,
    force: RunForceOption = False,
    debug: RunDebugOption = False,
    output_json: RunJsonOption = False,
) -> None:
    """Evaluate the implementor with no library mounted.

    Takes `environment.*` and `tasks_root` overrides.
    """
    setup = _setup(
        agent=agent,
        model=model,
        reasoning=reasoning,
        agent_version=agent_version,
        agent_kwargs=agent_kwargs,
        agent_env=agent_env,
        allowed_hosts=allowed_hosts,
        attempts=attempts,
        prompt=prompt,
        sandbox=Sandbox(cpus=cpus, memory_mb=memory_mb, storage_mb=storage_mb),
        overrides=overrides,
    )
    selection = select_task_problem_pairs(
        normalized_cli_filters(tasks, "--task"),
        normalized_cli_filters(problems, "--problem"),
        tasks_root=setup.tasks_root,
    )
    _evaluate(
        _job(setup, selection.problems, (NoLibrary(),), n_concurrent),
        output_dir=output_dir,
        name=run_name,
        force=force,
        debug=debug,
        output_json=output_json,
    )


@eval_app.command(name="existing-library")
def eval_existing_library(
    agent: AgentOption,
    model: ModelOption,
    overrides: OverridesArgument = None,
    existing_libraries: Annotated[
        list[str] | None,
        typer.Option(
            "--existing-library",
            help=(
                "Omit to run each selected task's spine; repeat to select "
                "specific declared libraries."
            ),
        ),
    ] = None,
    reasoning: RunReasoningOption = None,
    agent_version: RunAgentVersionOption = None,
    agent_kwargs: RunAgentKwargsOption = None,
    agent_env: RunAgentEnvOption = None,
    allowed_hosts: RunAllowedAgentHostsOption = None,
    attempts: AttemptsOption = 1,
    prompt: PromptOption = DEFAULT_PROMPT,
    cpus: CpusOption = 2,
    memory_mb: MemoryMbOption = 4096,
    storage_mb: StorageMbOption = 10240,
    output_dir: RunOutputDirOption = Path("runs"),
    run_name: RunNameOption = None,
    n_concurrent: RunConcurrencyOption = DEFAULT_CONCURRENCY,
    tasks: RunTasksOption = None,
    problems: RunProblemsOption = None,
    force: RunForceOption = False,
    debug: RunDebugOption = False,
    output_json: RunJsonOption = False,
) -> None:
    """Evaluate the implementor against the tasks' declared existing libraries.

    Takes `environment.*` and `tasks_root` overrides.
    """
    setup = _setup(
        agent=agent,
        model=model,
        reasoning=reasoning,
        agent_version=agent_version,
        agent_kwargs=agent_kwargs,
        agent_env=agent_env,
        allowed_hosts=allowed_hosts,
        attempts=attempts,
        prompt=prompt,
        sandbox=Sandbox(cpus=cpus, memory_mb=memory_mb, storage_mb=storage_mb),
        overrides=overrides,
    )
    selection = select_task_problem_pairs(
        normalized_cli_filters(tasks, "--task"),
        normalized_cli_filters(problems, "--problem"),
        tasks_root=setup.tasks_root,
    )
    selected_libraries = normalized_cli_filters(
        existing_libraries, "--existing-library"
    )
    declared = {name for task in selection.tasks for name in task.existing_libraries}
    if unknown := set(selected_libraries) - declared:
        raise typer.BadParameter(
            "Selected tasks do not declare existing library/libraries: "
            + ", ".join(sorted(unknown))
        )
    conditions = tuple(
        ExistingLibrary(task=task, name=name, entry=entry)
        for task in selection.tasks
        for name, entry in selected_existing_library_entries(task, selected_libraries)
    )
    _evaluate(
        _job(setup, selection.problems, conditions, n_concurrent),
        output_dir=output_dir,
        name=run_name,
        force=force,
        debug=debug,
        output_json=output_json,
    )


def selected_existing_library_entries(
    task: Task, selected_libraries: tuple[str, ...]
) -> tuple[tuple[str, ExistingLibraryEntry], ...]:
    """Resolve explicit comparators or a task's default spine comparator."""
    if selected_libraries:
        return tuple(
            (name, entry)
            for name, entry in task.existing_libraries.items()
            if name in selected_libraries
        )
    if task.spine is None:
        return ()
    return ((task.spine, task.existing_libraries[task.spine]),)


def _setup(
    *,
    agent: str,
    model: str,
    reasoning: ReasoningLevel | None,
    agent_version: str | None,
    agent_kwargs: list[str] | None,
    agent_env: list[str] | None,
    allowed_hosts: list[str] | None,
    attempts: int,
    prompt: Path,
    sandbox: Sandbox,
    overrides: list[str] | None,
) -> _Setup:
    """Resolve the command line into the one implementor's evaluation settings."""
    document = override_document(
        overrides, allowed=frozenset({"environment", "tasks_root"})
    )
    implementor = live_agent(
        agent,
        model,
        reasoning=reasoning,
        agent_version=agent_version,
        agent_kwargs=agent_kwargs,
        env=agent_environment(agent_env),
        allowed_hosts=allowed_agent_hosts(allowed_hosts),
    )
    try:
        settings = EvaluationSettings(
            agents={arm_label(implementor): implementor},
            attempts=attempts,
            prompt=prompt.read_text(encoding="utf-8"),
            sandbox=sandbox,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--prompt") from error
    tasks_root = document.get("tasks_root")
    return _Setup(
        settings=settings,
        environment=sandbox_environment(override_environment(document), sandbox),
        tasks_root=None if tasks_root is None else Path(tasks_root),
    )


def _job(
    setup: _Setup,
    problems: tuple[Problem, ...],
    conditions: tuple[LibraryCondition, ...],
    n_concurrent: int,
) -> Job:
    """Cross the implementor with every standalone condition."""
    return Job(
        problems=problems,
        arms=tuple(
            Arm(label=key, condition=condition, agent=agent)
            for key, agent in setup.settings.agents.items()
            for condition in conditions
        ),
        prompt_template=setup.settings.prompt,
        attempts=tuple(range(1, setup.settings.attempts + 1)),
        n_concurrent=n_concurrent,
        environment=setup.environment,
    )


def _log_evaluation_start(
    request: Job, run_dir: Path, *, debug_build_contexts: bool
) -> None:
    """Log the safe startup parameters and durable result location."""
    agent = request.arms[0].agent
    logger.info(
        "Starting Evaluation Run; results will be saved to the run directory.",
        run_dir=run_dir.as_posix(),
        agent=agent.name or agent.import_path,
        model=agent.model_name or "dummy",
        task_count=len(request.tasks),
        problem_count=len(request.problems),
        arm_count=len(request.arms),
        attempt_labels=request.attempts,
        n_concurrent=request.n_concurrent,
        debug_build_contexts=debug_build_contexts,
    )


def _evaluate(
    request: Job,
    *,
    output_dir: Path,
    name: str | None,
    force: bool,
    debug: bool,
    output_json: bool,
) -> None:
    """Plan, run, and finalize one new Evaluation Run, then report it."""
    if output_json:
        route_console_to_stderr()
    try:
        run_dir = new_run_directory(output_dir, name, kind="evaluation")
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    _log_evaluation_start(request, run_dir, debug_build_contexts=debug)
    report = execute(request, run_dir, force=force, debug_build_contexts=debug)
    display_evaluation(report, json_output=output_json, result_dir=run_dir)
