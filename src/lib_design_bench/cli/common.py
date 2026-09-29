"""Shared command-boundary helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal

import structlog
import typer
import yaml
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import normalize_allowed_hosts
from harbor.models.trial.config import AgentConfig as HarborAgentConfig
from harbor.models.trial.config import EnvironmentConfig

from lib_design_bench.cli.display import display_evaluation_scores
from lib_design_bench.common import ReasoningLevel
from lib_design_bench.common import get_tasks_dir
from lib_design_bench.common import now_timestamp
from lib_design_bench.common import safe_path_part
from lib_design_bench.models import Task
from lib_design_bench.models.experiment import ConfigOverride
from lib_design_bench.models.experiment import EnvironmentSettings
from lib_design_bench.models.experiment import Sandbox
from lib_design_bench.models.experiment import apply_overrides
from lib_design_bench.models.reports import RESULT_FILE_NAME
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.models.reports import RunReport
from lib_design_bench.models.task import Problem
from lib_design_bench.runs.store import Run

logger = structlog.get_logger(__name__)


_ENVIRONMENT_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


DEFAULT_CONCURRENCY = 4
"""Trials a new run executes at once; a continued one reuses its own."""


def echo_result_json(result_dir: Path) -> None:
    """Print the result saved at one result owner."""
    typer.echo(LdbResult.load(result_dir / RESULT_FILE_NAME).to_json(), nl=False)


def display_evaluation(
    report: RunReport, *, json_output: bool, result_dir: Path
) -> None:
    """Print a finalized evaluation run's saved result, or its score table."""
    if json_output:
        echo_result_json(result_dir)
    else:
        display_evaluation_scores(report)


def default_sandbox_environment(overrides: Sequence[str] | None) -> EnvironmentConfig:
    """Resolve a command's `environment.*` overrides at Harbor's task sandbox size."""
    return environment_config(
        override_environment(
            override_document(overrides, allowed=frozenset({"environment"}))
        ),
        cpus=None,
        memory_mb=None,
        storage_mb=None,
    )


RunReasoningOption = Annotated[
    ReasoningLevel | None,
    typer.Option(
        "--reasoning",
        help="Reasoning effort. Omit to leave unset.",
        case_sensitive=False,
    ),
]


RunAgentVersionOption = Annotated[
    str | None,
    typer.Option(
        "--agent-version",
        help="Pin the agent CLI install version (e.g. claude-code@1.2.3).",
    ),
]


RunAgentKwargsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--agent-kwargs",
        help=(
            "Agent keyword argument (`KEY=VALUE`), read as a YAML value and "
            "passed straight to Harbor. Repeat to set multiple."
        ),
    ),
]


RunAgentEnvOption = Annotated[
    list[str] | None,
    typer.Option(
        "--agent-env",
        help=(
            "Environment assignment (`NAME=VALUE`) for every agent in the run. "
            "Repeat to set multiple."
        ),
    ),
]


RunAllowedAgentHostsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--allow-agent-host",
        help="Host added to every agent's network allowlist. Repeat to add multiple.",
    ),
]


RunConcurrencyOption = Annotated[
    int,
    typer.Option("--n-concurrent", "-n", help="Maximum concurrent trials."),
]


RunOutputDirOption = Annotated[
    Path,
    typer.Option(
        "--output",
        "-o",
        help="Root output directory for runs.",
        readable=True,
        writable=True,
        file_okay=False,
    ),
]


RunNameOption = Annotated[
    str | None,
    typer.Option(
        "--name",
        help="Directory name for the new run below --output. Omit to use the start time.",
    ),
]


RunJsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Print the persisted run report as JSON instead of the score table.",
    ),
]


RunTasksOption = Annotated[
    list[str] | None,
    typer.Option("--task", help="Only run the named task(s)."),
]


RunProblemsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--problem",
        help=(
            "Only run an active Evaluation Phase problem. Repeat with either NAME or "
            "TASK/NAME to select one pair across tasks."
        ),
    ),
]


RunImplementorsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--eval-agent",
        help=(
            "Only run this implementor key from the config's `evaluation.agents`. "
            "Repeat to select several."
        ),
    ),
]


RunDebugOption = Annotated[
    bool,
    typer.Option(
        "--debug",
        help="Keep materialized build contexts under the run directory for inspection.",
    ),
]


OverridesArgument = Annotated[
    list[str] | None,
    typer.Argument(
        help=(
            "Config overrides as dotted `KEY=VALUE` pairs, each value read as "
            "YAML (e.g. `evaluation.attempts=2 environment.type=modal`)."
        ),
        show_default=False,
    ),
]


RunForceOption = Annotated[
    bool,
    typer.Option("--force", help="Replace a stale run lease after confirming idle."),
]


def normalized_cli_filters(values: list[str] | None, option: str) -> tuple[str, ...]:
    """Trim and deduplicate one repeatable string option."""
    if values is None:
        return ()
    selected: list[str] = []
    for value in values:
        stripped = value.strip()
        if not stripped:
            raise typer.BadParameter(f"`{option}` values must not be empty.")
        if stripped not in selected:
            selected.append(stripped)
    return tuple(selected)


def config_overrides(values: Sequence[str] | None) -> tuple[ConfigOverride, ...]:
    """Parse `KEY=VALUE` arguments whose keys are dotted paths from the config root.

    Only the syntax is checked here; which keys exist is the config model's
    own question, so those rejections come from validating the result.
    """
    overrides: list[ConfigOverride] = []
    for value in values or ():
        target, parsed = _assignment(value, "KEY=VALUE")
        segments = tuple(segment.strip() for segment in target.split("."))
        if not all(segments):
            raise typer.BadParameter(
                f"Override keys must be dotted paths such as `a.b.c`: {value!r}",
                param_hint="KEY=VALUE",
            )
        overrides.append(ConfigOverride(text=value, path=segments, value=parsed))
    return tuple(overrides)


def override_document(
    values: Sequence[str] | None, *, allowed: frozenset[str]
) -> dict[str, Any]:
    """Apply overrides to an empty document, admitting only `allowed` root keys.

    Commands without a config file of their own still take overrides for the
    few settings they read, and reject any key that would configure nothing.
    """
    overrides = config_overrides(values)
    rejected = sorted(
        override.text for override in overrides if override.path[0] not in allowed
    )
    if rejected:
        raise typer.BadParameter(
            f"This command only accepts overrides under {', '.join(sorted(allowed))}: "
            f"{', '.join(rejected)}",
            param_hint="KEY=VALUE",
        )
    document: dict[str, Any] = {}
    try:
        apply_overrides(document, overrides)
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="KEY=VALUE") from error
    return document


def _assignment(value: str, option: str) -> tuple[str, Any]:
    """Split one `TARGET=VALUE` option into its target and its YAML scalar."""
    target, separator, raw = value.partition("=")
    if not separator or not target.strip():
        raise typer.BadParameter(
            f"`{option}` values must use `TARGET=VALUE` syntax: {value!r}",
            param_hint=option,
        )
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise typer.BadParameter(
            f"`{option}` value is not a YAML scalar: {raw!r} ({error})",
            param_hint=option,
        ) from error
    return target.strip(), parsed


def environment_config(
    settings: EnvironmentSettings,
    *,
    cpus: int | None,
    memory_mb: int | None,
    storage_mb: int | None,
) -> EnvironmentConfig:
    """Build a Harbor environment configuration from validated provider settings."""
    kwargs = dict(settings.kwargs)
    if settings.type is EnvironmentType.DAYTONA:
        if any(size is not None and size % 1024 for size in (memory_mb, storage_mb)):
            raise typer.BadParameter(
                "Daytona sandbox memory_mb and storage_mb must be multiples of "
                f"1024: memory_mb={memory_mb}, storage_mb={storage_mb}",
                param_hint="KEY=VALUE",
            )
        kwargs.setdefault("auto_snapshot", True)
    if settings.type is EnvironmentType.MODAL:
        kwargs.setdefault("modal_vm_runtime", True)
        kwargs.setdefault("sandbox_timeout_secs", 6 * 60 * 60)
    return EnvironmentConfig(
        type=settings.type,
        kwargs=kwargs,
        override_cpus=cpus,
        override_memory_mb=memory_mb,
        override_storage_mb=storage_mb,
    )


def override_environment(document: dict[str, Any]) -> EnvironmentSettings:
    """Validate the `environment.*` overrides of a command without a config."""
    try:
        return EnvironmentSettings.model_validate(document.get("environment", {}))
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="KEY=VALUE") from error


def sandbox_environment(
    environment: EnvironmentSettings, sandbox: Sandbox
) -> EnvironmentConfig:
    """Combine the configured provider with one phase's declared sandbox size."""
    return environment_config(
        environment,
        cpus=sandbox.cpus,
        memory_mb=sandbox.memory_mb,
        storage_mb=sandbox.storage_mb,
    )


def replacement_environment(
    saved: EnvironmentType | None, overrides: object
) -> EnvironmentSettings:
    """Resolve `environment.*` overrides against a run's saved provider.

    Provider options are replaced rather than merged, since one provider's
    options mean nothing to another; the provider itself is kept unless
    `environment.type` names a new one.
    """
    if not isinstance(overrides, dict):
        raise typer.BadParameter(
            f"`environment` overrides must set keys such as `environment.type`: "
            f"{overrides!r}",
            param_hint="KEY=VALUE",
        )
    if saved is None and "type" not in overrides:
        raise typer.BadParameter(
            "Set `environment.type` when replacing a custom provider's options.",
            param_hint="KEY=VALUE",
        )
    try:
        return EnvironmentSettings.model_validate({"type": saved, **overrides})
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="KEY=VALUE") from error


def load_tasks(names: Sequence[str], *, tasks_root: Path | None) -> tuple[Task, ...]:
    """Load named tasks, or every declared task when `names` is empty."""
    task_root = (tasks_root or get_tasks_dir()).expanduser().resolve()
    resolved_names = tuple(names) or tuple(
        sorted(
            path.name
            for path in task_root.iterdir()
            if path.is_dir() and (path / "task.yaml").is_file()
        )
    )
    return tuple(
        sorted(
            (Task.from_dir(task_root / name) for name in resolved_names),
            key=lambda task: task.name,
        )
    )


def select_problems(
    tasks: tuple[Task, ...], names: tuple[str, ...]
) -> tuple[Problem, ...]:
    """Resolve requested bare problem names against each selected task."""
    if not names:
        return tuple(problem for task in tasks for problem in task.active_problems())
    return tuple(task.problem(name) for task in tasks for name in names)


@dataclass(frozen=True)
class TaskProblemSelection:
    """Resolved task scope and active problem pairs for a CLI evaluation."""

    tasks: tuple[Task, ...]
    problems: tuple[Problem, ...]


def select_task_problem_pairs(
    task_names: tuple[str, ...],
    problem_selectors: tuple[str, ...],
    *,
    tasks_root: Path | None,
) -> TaskProblemSelection:
    """Load only the tasks the selectors can name, then resolve the selectors.

    Without `--task`, qualified selectors alone load just the tasks they name.
    """
    qualified_task_names = tuple(
        dict.fromkeys(
            _qualified_problem_selector(selector)[0]
            for selector in problem_selectors
            if "/" in selector
        )
    )
    only_qualified = all("/" in selector for selector in problem_selectors)
    names_to_load = task_names or (qualified_task_names if only_qualified else ())
    task_root = (tasks_root or get_tasks_dir()).expanduser().resolve()
    unknown_task_names = tuple(
        name for name in names_to_load if not (task_root / name / "task.yaml").is_file()
    )
    if unknown_task_names:
        raise typer.BadParameter(
            "Unknown task(s): " + ", ".join(unknown_task_names),
            param_hint="--task" if task_names else "--problem",
        )
    return select_task_problem_pairs_from_tasks(
        load_tasks(names_to_load, tasks_root=task_root),
        task_names,
        problem_selectors,
    )


def select_task_problem_pairs_from_tasks(
    available_tasks: tuple[Task, ...],
    task_names: tuple[str, ...],
    problem_selectors: tuple[str, ...],
) -> TaskProblemSelection:
    """Resolve selectors against one supplied task population."""
    by_name = {task.name: task for task in available_tasks}
    unknown_task_names = tuple(name for name in task_names if name not in by_name)
    if unknown_task_names:
        raise typer.BadParameter(
            "Unknown task(s): " + ", ".join(unknown_task_names),
            param_hint="--task",
        )
    qualified = tuple(selector for selector in problem_selectors if "/" in selector)
    bare = tuple(selector for selector in problem_selectors if "/" not in selector)
    qualified_task_names = tuple(
        dict.fromkeys(
            _qualified_problem_selector(selector)[0] for selector in qualified
        )
    )
    conflicts = tuple(
        name for name in qualified_task_names if task_names and name not in task_names
    )
    if conflicts:
        raise typer.BadParameter(
            "Qualified `--problem` selectors conflict with `--task`: "
            + ", ".join(conflicts),
            param_hint="--problem",
        )
    unknown_qualified_task_names = tuple(
        name for name in qualified_task_names if name not in by_name
    )
    if unknown_qualified_task_names:
        raise typer.BadParameter(
            "Unknown task(s) in `--problem`: "
            + ", ".join(unknown_qualified_task_names),
            param_hint="--problem",
        )
    if task_names:
        tasks = tuple(by_name[name] for name in task_names)
    elif qualified_task_names and not bare:
        tasks = tuple(by_name[name] for name in qualified_task_names)
    else:
        tasks = available_tasks

    if not problem_selectors:
        return TaskProblemSelection(tasks=tasks, problems=select_problems(tasks, ()))

    selected: list[Problem] = []
    seen: set[tuple[str, str]] = set()
    for selector in qualified:
        task_name, problem_name = _qualified_problem_selector(selector)
        _add_selected_problem(selected, seen, by_name[task_name], problem_name)
    for task in tasks:
        for selector in bare:
            _add_selected_problem(selected, seen, task, selector)
    return TaskProblemSelection(tasks=tasks, problems=tuple(selected))


def _qualified_problem_selector(selector: str) -> tuple[str, str]:
    """Parse one `TASK/PROBLEM` CLI selector."""
    task_name, separator, problem_name = selector.partition("/")
    if not separator or not task_name or not problem_name or "/" in problem_name:
        raise typer.BadParameter(
            f"Qualified `--problem` selectors must use `TASK/PROBLEM`: {selector!r}",
            param_hint="--problem",
        )
    return task_name, problem_name


def _add_selected_problem(
    selected: list[Problem],
    seen: set[tuple[str, str]],
    task: Task,
    problem_name: str,
) -> None:
    """Append one pair once, preserving the selector's declared order."""
    if problem_name not in task.problems:
        raise typer.BadParameter(
            f"Unknown problem for task {task.name!r}: {problem_name!r}",
            param_hint="--problem",
        )
    problem = task.problem(problem_name)
    key = (task.name, problem.name)
    if key not in seen:
        selected.append(problem)
        seen.add(key)


def live_agent(
    agent: str,
    model: str,
    *,
    reasoning: ReasoningLevel | None,
    agent_version: str | None,
    agent_kwargs: Sequence[str] | None,
    env: Mapping[str, str],
    allowed_hosts: Sequence[str],
) -> HarborAgentConfig:
    """Build the complete Harbor configuration for a live CLI agent.

    `agent_kwargs` merges last and raw: Harbor owns which keyword arguments one
    agent accepts, so a command can reach an agent option this CLI has no flag
    for. A repeated key keeps its last value.
    """
    agent_name = _required_string(agent, "agent")
    model_name = _required_string(model, "model")
    version = _optional_string(agent_version, "agent_version")
    kwargs: dict[str, Any] = {}
    if reasoning is not None:
        kwargs["reasoning_effort"] = reasoning
    if version is not None:
        kwargs["version"] = version
    config = HarborAgentConfig(
        name=agent_name,
        model_name=model_name,
        env=dict(env),
        kwargs=kwargs,
        extra_allowed_hosts=list(allowed_hosts),
    )
    if config.name == "codex":
        kwargs["reasoning_summary"] = "detailed"
    overrides = dict(
        _assignment(value, "--agent-kwargs") for value in agent_kwargs or ()
    )
    return config.model_copy(update={"kwargs": {**kwargs, **overrides}})


def oracle_agent() -> HarborAgentConfig:
    """Build Harbor's known-correct oracle agent configuration."""
    return HarborAgentConfig(name="oracle", model_name="dummy")


def new_run_directory(
    output_root: Path, name: str | None, *, kind: Literal["run", "evaluation", "verify"]
) -> Path:
    """Return a new run's directory below a container.

    Without `name`, the directory is Harbor's default job name, the start time,
    prefixed with the kind of run: `evaluation_2026-09-29__14-03-11`.
    """
    output = output_root.expanduser().resolve()
    if Run.exists(output):
        raise ValueError(
            f"Output directory is an existing run: {output}. Use `ldb resume` to continue it."
        )
    if name is not None and name != safe_path_part(name):
        raise ValueError(
            f"Run name must be one path segment of letters, digits, `.`, `_`, or `-`: {name!r}"
        )
    run_dir = output / (f"{kind}_{now_timestamp()}" if name is None else name)
    if run_dir.exists():
        raise ValueError(
            f"Run directory already exists: {run_dir}. Choose another `--name` "
            "or continue that run in place."
        )
    return run_dir


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string: {value!r}")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must be non-empty")
    return normalized


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def agent_environment(values: Sequence[str] | None) -> dict[str, str]:
    """Parse repeated `--agent-env NAME=VALUE` options; a repeated name keeps its last value."""
    env: dict[str, str] = {}
    for assignment in values or ():
        name, separator, value = assignment.partition("=")
        if not separator or not _ENVIRONMENT_VARIABLE_NAME.fullmatch(name):
            raise typer.BadParameter(
                f"Agent environment assignments must use `NAME=VALUE`: {assignment!r}",
                param_hint="--agent-env",
            )
        env[name] = value
    return env


def allowed_agent_hosts(values: Sequence[str] | None) -> tuple[str, ...]:
    """Validate repeated `--allow-agent-host` options into Harbor host names."""
    try:
        return tuple(dict.fromkeys(normalize_allowed_hosts(list(values or ()))))
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--allow-agent-host") from error
