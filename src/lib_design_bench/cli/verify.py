"""Verifier-only commands: reference sweeps, static reference refresh, and replays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from typing import TypedDict

import typer

from lib_design_bench.cli.common import DEFAULT_CONCURRENCY
from lib_design_bench.cli.common import OverridesArgument
from lib_design_bench.cli.common import RunConcurrencyOption
from lib_design_bench.cli.common import RunForceOption
from lib_design_bench.cli.common import default_sandbox_environment
from lib_design_bench.cli.common import display_evaluation
from lib_design_bench.cli.common import environment_config
from lib_design_bench.cli.common import load_tasks
from lib_design_bench.cli.common import new_run_directory
from lib_design_bench.cli.common import normalized_cli_filters
from lib_design_bench.cli.common import oracle_agent
from lib_design_bench.cli.common import override_document
from lib_design_bench.cli.common import override_environment
from lib_design_bench.cli.common import select_problems
from lib_design_bench.cli.display import display_static_references
from lib_design_bench.logging import route_console_to_stderr
from lib_design_bench.models import Arm
from lib_design_bench.models import Job
from lib_design_bench.models import NoLibrary
from lib_design_bench.models import Problem
from lib_design_bench.models import ReplayJob
from lib_design_bench.models import RunReport
from lib_design_bench.models import StaticMetrics
from lib_design_bench.models import Task
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.job import AgentConfig
from lib_design_bench.models.job import arm_label
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.pipeline.references import refresh_static_references
from lib_design_bench.pipeline.replay import replay_and_update
from lib_design_bench.pipeline.run import execute
from lib_design_bench.runs.store import Run


class ArmResult(TypedDict):
    """JSON values for one present floor or ceiling arm."""

    reward: float | None
    pass_rate: float | None
    static: dict[str, float | None] | None


class ProblemSummary(TypedDict):
    """JSON values for one Evaluation Phase problem's declared arms."""

    no_library: ArmResult | None
    existing_libraries: dict[str, ArmResult | None]


class TaskVerificationSummary(TypedDict):
    """Compact JSON report for one task reference sweep."""

    task: str
    problems: dict[str, ProblemSummary]


def task_summary(
    task: Task, problems: tuple[Problem, ...], report: RunReport
) -> TaskVerificationSummary:
    """Summarize one finalized oracle run by its checked-in reference arms."""
    summary = _empty_summary(task, problems)
    for trial in report.attempts:
        problem = summary["problems"].get(trial.problem)
        if problem is not None:
            _set_arm(problem, trial.library, _arm_result(trial))
    return summary


def _empty_summary(
    task: Task, problems: tuple[Problem, ...]
) -> TaskVerificationSummary:
    """Return the declared arm shape before any verifier work has run."""
    result_problems: dict[str, ProblemSummary] = {
        problem.name: {
            "no_library": None,
            "existing_libraries": {
                library_name: None for library_name in problem.existing_libraries()
            },
        }
        for problem in problems
    }
    return {"task": task.name, "problems": result_problems}


def _arm_result(trial: TrialReport) -> ArmResult:
    return {
        "reward": trial.reward,
        "pass_rate": trial.pass_rate,
        "static": _static_scalars(trial.static_analysis),
    }


def _static_scalars(
    static: StaticMetrics | None,
) -> dict[str, float | None] | None:
    if static is None:
        return None
    return {
        name: float(value)
        for name, value in static.as_scalars().items()
        if value is not None
    }


def _set_arm(problem: ProblemSummary, library: str, value: ArmResult) -> None:
    if library == "no-library":
        problem["no_library"] = value
    elif library in problem["existing_libraries"]:
        problem["existing_libraries"][library] = value


def verify_task_command(
    task_name: Annotated[str, typer.Argument(help="Checked-in task name.")],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Root directory for paired reference verification output.",
            file_okay=False,
            dir_okay=True,
        ),
    ],
    overrides: OverridesArgument = None,
    problems: Annotated[
        list[str] | None,
        typer.Option("--problem", help="Verify only this active problem; repeatable."),
    ] = None,
    n_concurrent: RunConcurrencyOption = DEFAULT_CONCURRENCY,
    force: RunForceOption = False,
) -> None:
    """Verify one task's checked-in floor and declared ceiling references.

    Takes `environment.*` and `tasks_root` overrides; sandbox sizes are the
    Harbor task defaults.
    """
    settings = override_document(
        overrides, allowed=frozenset({"environment", "tasks_root"})
    )
    environment = override_environment(settings)
    tasks_root = settings.get("tasks_root")
    (task,) = load_tasks(
        (task_name,), tasks_root=None if tasks_root is None else Path(tasks_root)
    )
    selected = normalized_cli_filters(problems, "--problem")
    selected_problems = select_problems((task,), selected)
    agent = oracle_agent()
    arms = verification_arms(task, selected_problems, agent)
    request = Job(
        problems=selected_problems,
        arms=arms,
        attempts=(1,),
        n_concurrent=n_concurrent,
        environment=environment_config(
            environment, cpus=None, memory_mb=None, storage_mb=None
        ),
    )
    output = output_dir.expanduser().resolve()
    try:
        run_dir = (
            output
            if Run.exists(output)
            else new_run_directory(output, None, kind="verify")
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    report = execute(request, run_dir, force=force, debug_build_contexts=False)
    typer.echo(json.dumps(task_summary(task, selected_problems, report), indent=2))


def verification_arms(
    task: Task, problems: tuple[Problem, ...], agent: AgentConfig
) -> tuple[Arm, ...]:
    """Build oracle arms only for checked-in solution pairs."""
    floor_problems = tuple(
        problem.name
        for problem in problems
        if problem.solution_dir("no-library").is_dir()
    )
    label = arm_label(agent)
    existing_arms = tuple(
        Arm(
            label=label,
            condition=ExistingLibrary(
                task=task,
                name=name,
                entry=entry,
                problems=reference_problems,
            ),
            agent=agent,
        )
        for name, entry in task.existing_libraries.items()
        for reference_problems in (
            tuple(
                problem.name
                for problem in problems
                if problem.solution_dir(name).is_dir()
            ),
        )
        if reference_problems
    )
    return (
        (Arm(label=label, condition=NoLibrary(problems=floor_problems), agent=agent),)
        if floor_problems
        else ()
    ) + existing_arms


def verify_run_command(
    source: Annotated[
        Path,
        typer.Argument(
            help="Saved run directory.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    overrides: OverridesArgument = None,
    n_concurrent: RunConcurrencyOption = DEFAULT_CONCURRENCY,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Replay output directory; defaults to `<source>/verify`.",
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
    harbor_task: Annotated[
        Path | None,
        typer.Option(
            "--harbor-task",
            help="Current Harbor task override.",
            file_okay=False,
            dir_okay=True,
            exists=True,
            resolve_path=True,
        ),
    ] = None,
    problems: Annotated[
        list[str] | None,
        typer.Option(
            "--problem",
            help="Verify only this problem across selected trials; repeatable.",
        ),
    ] = None,
    task_name: Annotated[
        str | None,
        typer.Option("--task", help="Verify only trials for this task."),
    ] = None,
    trial_name: Annotated[
        str | None,
        typer.Option("--trial", help="Verify only this persisted trial."),
    ] = None,
    update: Annotated[
        bool,
        typer.Option("--update", help="Replace source verifier results after success."),
    ] = False,
    force: RunForceOption = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print only the finalized report JSON."),
    ] = False,
) -> None:
    """Replay saved artifacts against their Harbor tasks' current verifiers.

    Takes `environment.*` overrides; sandbox sizes are the Harbor task defaults.
    """
    if json_output:
        route_console_to_stderr()
    if update and output_dir is not None:
        raise typer.BadParameter("`--update` cannot be combined with `--output`.")
    if update and harbor_task is not None:
        raise typer.BadParameter("`--update` cannot be combined with `--harbor-task`.")
    request = ReplayJob(
        source=source,
        tasks=normalized_cli_filters(
            None if task_name is None else [task_name], "--task"
        ),
        trials=normalized_cli_filters(
            None if trial_name is None else [trial_name], "--trial"
        ),
        problems=normalized_cli_filters(problems, "--problem"),
        task_override=harbor_task,
        n_concurrent=n_concurrent,
        environment=default_sandbox_environment(overrides),
    )
    if update:
        report = replay_and_update(request, Run.open(source), force=force)
    else:
        output = output_dir or source / "verify"
        try:
            run_dir = (
                output
                if Run.exists(output)
                else new_run_directory(output, None, kind="verify")
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        report = execute(request, run_dir, force=force, debug_build_contexts=False)
    saved = Run.open(source if update else run_dir)
    display_evaluation(
        report, json_output=json_output, result_dir=saved.result_owner().dir
    )


app = typer.Typer(
    help="Run current verifiers against reference solutions or saved artifacts.",
    no_args_is_help=True,
)


app.command(name="task")(verify_task_command)


app.command(name="run")(verify_run_command)


def static_command(
    directory: Annotated[
        Path,
        typer.Argument(
            help="Directory searched recursively for tasks whose references to measure.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    overrides: OverridesArgument = None,
    n_concurrent: RunConcurrencyOption = DEFAULT_CONCURRENCY,
) -> None:
    """Remeasure every task's static reference in its task verifier.

    Takes `environment.*` overrides for the trials that measure; sandbox sizes
    are the Harbor task defaults. `ldb recalculate` remeasures a saved run.
    """
    try:
        rows = refresh_static_references(
            directory,
            oracle=oracle_agent(),
            n_concurrent=n_concurrent,
            environment=default_sandbox_environment(overrides),
        )
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="directory") from error
    display_static_references(rows)
