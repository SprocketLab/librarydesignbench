"""Plan runs: persist requests, render prompts, and grow an experiment's cells."""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from datetime import UTC
from datetime import datetime
from pathlib import Path

import structlog
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import EnvironmentConfig
from jinja2 import Environment
from jinja2 import StrictUndefined
from jinja2 import meta

from lib_design_bench.common import get_repo_root
from lib_design_bench.common import hash_paths
from lib_design_bench.models import LIBRARY_INSTALL
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.conditions import LibraryCondition
from lib_design_bench.models.experiment import EvaluationSettings
from lib_design_bench.models.experiment import ExperimentConfig
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import Request
from lib_design_bench.models.manifest import DesignLaunch
from lib_design_bench.models.manifest import ExperimentRecord
from lib_design_bench.models.manifest import RunManifest
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.task import WORKSPACE_LOCATION
from lib_design_bench.models.task import Problem
from lib_design_bench.models.task import Task
from lib_design_bench.runs.outcomes import classify
from lib_design_bench.runs.outcomes import launches_in
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import expand
from lib_design_bench.runs.store import initialize_run_outputs
from lib_design_bench.runs.store import read_authored_artifact

logger = structlog.get_logger(__name__)


_INSTRUCTION_PATTERN = re.compile(r"{{\s*instruction\s*}}")


_INSTRUCTION_SENTINEL = "__LIB_DESIGN_BENCH_INSTRUCTION_PLACEHOLDER__"


_ALLOWED_TEMPLATE_VARIABLES = {
    "library_name",
    "library_install",
    "workspace_dir",
    "instruction",
    "runtime",
    "libraries",
}


def repo_commit() -> str:
    """Return the current git commit, or ``unknown`` outside a checkout."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=get_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Repo commit is unavailable", stderr=result.stderr.strip())
        return "unknown"
    return result.stdout.strip() or "unknown"


def hash_tasks(tasks: tuple[Task, ...]) -> str:
    """Hash the selected task source directories."""
    return hash_paths(tuple(task.source_dir for task in tasks))


def render_prompt_template(
    template_text: str,
    task: Task,
    *,
    library_name: str | None = None,
    available_libraries: tuple[str, ...] = (),
) -> str:
    """Render an LDB prompt template while preserving `{{instruction}}`.

    Rendering uses Jinja `StrictUndefined` and rejects any variable outside the
    prompt allowlist.
    """
    normalized = _INSTRUCTION_PATTERN.sub(_INSTRUCTION_SENTINEL, template_text)
    libraries = {
        f"`{name}`"
        for name in (*task.environment_dependencies, *available_libraries)
        if name
    }
    env = Environment(undefined=StrictUndefined)
    unsupported = meta.find_undeclared_variables(env.parse(normalized)) - (
        _ALLOWED_TEMPLATE_VARIABLES - {"instruction"}
    )
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ValueError(f"Unsupported prompt template variable(s): {names}")
    rendered = env.from_string(normalized).render(
        library_name=library_name or task.library_name,
        library_install=LIBRARY_INSTALL,
        workspace_dir=WORKSPACE_LOCATION,
        runtime=task.environment_runtime,
        libraries=", ".join(sorted(libraries, key=str.casefold)) or "none",
    )
    restored = rendered.replace(_INSTRUCTION_SENTINEL, "{{instruction}}")
    if "{{instruction}}" not in restored:
        raise ValueError(
            "Rendered prompt template must contain `{{instruction}}` for Harbor."
        )
    return restored


def render_prompt(
    task: Task,
    template: str,
    condition: LibraryCondition | None,
) -> str:
    """Render one planned launch prompt for its library condition."""
    if isinstance(condition, ExistingLibrary):
        available_libraries = (
            condition.name,
            *condition.entry.dependent_libraries,
            *condition.entry.related_dependencies,
        )
        return render_prompt_template(
            template,
            task,
            library_name=condition.name,
            available_libraries=available_libraries,
        )
    if isinstance(condition, AuthoredArtifact):
        return render_prompt_template(
            template,
            task,
            available_libraries=(condition.task.library_name,),
        )
    return render_prompt_template(template, task)


def plan(
    request: Request,
    run_dir: Path,
    *,
    experiment: ExperimentRecord | None = None,
) -> tuple[TrialLaunch, ...]:
    """Return unfinished launches, creating a self-describing run when needed.

    An existing run is planned from its own manifest. The persisted request is
    the authority, so hand-editing it to change a task set, an arm, or a
    library condition is honored by the next invocation and `request` is not compared
    to it. Task sources that changed since the run was planned are a logged
    warning rather than a refusal, because the manifest still states what this
    run measures and its recorded hash stays the provenance of what it was
    planned against.

    `experiment` records the published setup a new experiment run was launched
    from. An existing run keeps the record it was created with.
    """
    if Run.exists(run_dir):
        existing = Run.open(run_dir)
        _warn_on_changed_task_sources(existing)
        return launches_in(existing, "rerun")

    timestamp = datetime.now(UTC)
    return persist_request(
        run_dir,
        RunManifest(
            repo_commit=repo_commit(),
            tasks_hash=hash_tasks(
                _launch_tasks(expand(request, run_timestamp=timestamp))
            ),
            timestamp=timestamp,
            request=request,
            experiment=experiment,
        ),
    )


def persist_request(run_dir: Path, manifest: RunManifest) -> tuple[TrialLaunch, ...]:
    """Persist one run's manifest, execution identity, and rendered prompts.

    Creating a run and widening an existing one share this path, so trials a
    run gained later describe themselves exactly as the ones it started with.
    """
    run = Run.create(run_dir, manifest)
    launches = run.launches()
    initialize_run_outputs(run_dir, manifest.request, launches)
    _write_prompts(launches, run_dir)
    return launches


def _warn_on_changed_task_sources(existing: Run) -> None:
    """Report checked-in task sources that moved on since planning."""
    request = existing.request()
    if not isinstance(request, (Job, AuthorJob)):
        return
    current = hash_tasks(request.tasks)
    if current == existing.manifest.tasks_hash:
        return
    logger.warning(
        "Task sources changed since this run was planned; its manifest "
        "still decides what runs, and new work is graded by current tests.",
        run_dir=existing.dir.as_posix(),
        planned_tasks_hash=existing.manifest.tasks_hash,
        current_tasks_hash=current,
        tasks=[task.name for task in request.tasks],
    )


def _launch_tasks(launches: tuple[TrialLaunch, ...]) -> tuple[Task, ...]:
    return tuple({launch.task.source_dir: launch.task for launch in launches}.values())


def _write_prompts(launches: tuple[TrialLaunch, ...], run_dir: Path) -> None:
    written: set[Path] = set()
    for launch in launches:
        path = launch.prompt_path(run_dir)
        template = launch.prompt_template
        if path is None or template is None or path in written:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_prompt(launch.task, template, _condition(launch)),
            encoding="utf-8",
        )
        written.add(path)


def _condition(launch: TrialLaunch) -> LibraryCondition | None:
    return None if isinstance(launch, DesignLaunch) else launch.arm.condition


def authored_arms(
    *,
    source_run: Run,
    launches: tuple[DesignLaunch, ...],
    problems: tuple[Problem, ...],
    label: str,
    agent: AgentConfig,
) -> tuple[Arm, ...]:
    """Create library-backed arms for every selected problem.

    `label` names the implementor these arms belong to, so crossing several
    implementors over one design run keeps their cells apart.
    """
    selected = _selected_problems_by_task(problems)
    logger.debug(
        "Planning authored library arms.",
        source_run=source_run.dir.as_posix(),
        author_launch_count=len(launches),
        selected_task_count=len(selected),
        label=label,
    )
    arms: list[Arm] = []
    for launch in launches:
        task_problems = selected.get(launch.task.name)
        if task_problems is None:
            continue
        task, names = task_problems
        arms.append(
            _arm(
                source_run=source_run,
                launch=launch,
                task=task,
                problems=names,
                label=label,
                agent=agent,
            )
        )
    return tuple(arms)


def _selected_problems_by_task(
    problems: tuple[Problem, ...],
) -> dict[str, tuple[Task, tuple[str, ...]]]:
    selected: dict[str, tuple[Task, list[str]]] = {}
    for problem in problems:
        _task, names = selected.setdefault(problem.task.name, (problem.task, []))
        names.append(problem.name)
    return {name: (task, tuple(names)) for name, (task, names) in selected.items()}


def _arm(
    *,
    source_run: Run,
    launch: DesignLaunch,
    task: Task,
    problems: tuple[str, ...],
    label: str,
    agent: AgentConfig,
) -> Arm:
    artifact = read_authored_artifact(source_run, launch)
    return Arm(
        label=label,
        condition=AuthoredArtifact(
            task=task,
            source=artifact.workspace,
            attempt=launch.attempt,
            problems=problems,
            incomplete_reason=artifact.incomplete_reason,
        ),
        agent=agent,
    )


def grow_evaluation_run(
    *,
    design_run: Run,
    evaluation_dir: Path,
    config: ExperimentConfig,
    problems: tuple[Problem, ...],
    n_concurrent: int,
    environment: EnvironmentConfig,
) -> Job | None:
    """Return the evaluation request covering every task past its Design Phase.

    The derived request crosses every implementor the config declares with the
    libraries of every task whose author trials have all settled,
    over each of that task's problems and every evaluation attempt.

    An evaluation run that already exists keeps every problem and arm it
    persisted, exactly and in order; only the ones it lacks are appended, and
    execution settings follow the current invocation. Cell names carry the
    task, problem, arm label, and attempt, so a grown run leaves the names
    and slots of its earlier cells alone.

    `None` means no task has finished authoring yet and there is nothing to
    plan; a run that already exists still receives execution settings.
    """
    derived = evaluation_job(
        design_run=design_run,
        settings=config.evaluation,
        problems=problems,
        n_concurrent=n_concurrent,
        environment=environment,
    )
    if not Run.exists(evaluation_dir):
        return derived
    persisted = Run.open(evaluation_dir)
    request = persisted.request()
    if not isinstance(request, Job):
        raise ValueError(f"Not an evaluation run: {evaluation_dir}")
    grown = (request if derived is None else _union(request, derived)).model_copy(
        update={"environment": environment}
    )
    if grown == request:
        logger.debug(
            "The evaluation run already holds every cell design work supports.",
            evaluation_dir=evaluation_dir.as_posix(),
            arm_count=len(request.arms),
            problem_count=len(request.problems),
        )
        return request
    logger.info(
        "Updating evaluation cells and execution settings.",
        evaluation_dir=evaluation_dir.as_posix(),
        previous_environment=request.environment.type,
        environment=grown.environment.type,
        arm_count=len(grown.arms),
        added_arms=[arm.name() for arm in grown.arms[len(request.arms) :]],
        added_problems=[
            f"{problem.task.name}/{problem.name}"
            for problem in grown.problems[len(request.problems) :]
        ],
    )
    persist_request(
        evaluation_dir, persisted.manifest.model_copy(update={"request": grown})
    )
    return grown


def evaluation_job(
    *,
    design_run: Run,
    settings: EvaluationSettings,
    problems: tuple[Problem, ...],
    n_concurrent: int,
    environment: EnvironmentConfig,
) -> Job | None:
    """Cross every implementor with the libraries of every settled task.

    `None` means no selected task has finished its Design Phase yet.
    """
    launches = _settled_author_launches(design_run)
    authored = {launch.task.name for launch in launches}
    scoped = tuple(problem for problem in problems if problem.task.name in authored)
    if not scoped:
        logger.info(
            "No task has finished its Design Phase, so the experiment plans no cell yet.",
            design_run=design_run.dir.as_posix(),
        )
        return None
    arms = tuple(
        arm
        for implementor, agent in settings.agents.items()
        for arm in authored_arms(
            source_run=design_run,
            launches=launches,
            problems=scoped,
            label=implementor,
            agent=agent,
        )
    )
    return Job(
        problems=scoped,
        arms=arms,
        prompt_template=settings.prompt,
        attempts=tuple(range(1, settings.attempts + 1)),
        n_concurrent=n_concurrent,
        environment=environment,
    )


def _settled_author_launches(design_run: Run) -> tuple[DesignLaunch, ...]:
    """Return the author launches of every task whose attempts all settled.

    One attempt still in the rerun class holds its whole task back: the
    cells of that task mount every attempt's libraries, and a rerun
    replaces the ones it would have mounted.
    """
    attempts: dict[str, list[DesignLaunch]] = defaultdict(list)
    for launch in design_run.launches():
        if isinstance(launch, DesignLaunch):
            attempts[launch.task.name].append(launch)
    settled: list[DesignLaunch] = []
    for task, launches in attempts.items():
        outstanding = [
            launch.trial_name
            for launch in launches
            if classify(design_run.slot(launch)).outcome == "rerun"
        ]
        if outstanding:
            logger.info(
                "Holding a task's cells back until its author trials finish.",
                design_run=design_run.dir.as_posix(),
                task=task,
                rerun_trials=outstanding,
            )
            continue
        settled.extend(launches)
    logger.debug(
        "Selected the author trials whose libraries cells may mount.",
        design_run=design_run.dir.as_posix(),
        task_count=len({launch.task.name for launch in settled}),
        author_trial_count=len(settled),
    )
    return tuple(settled)


def _union(persisted: Job, derived: Job) -> Job:
    """Append the derived problems and arms the persisted request lacks.

    Arms are compared by the pair that names a cell, implementor label and
    condition name within a task, so an arm whose agent was hand-edited on
    disk stays as it was written rather than being planned a second time. One
    field is refreshed: an authored condition persisted while its library was
    unavailable takes the derived condition once the library exists, so the
    cells it holds run instead of publishing the stale incomplete reason.
    """
    known_problems = {
        (problem.task.source_dir, problem.name) for problem in persisted.problems
    }
    unknown_arms = {_arm_key(arm): arm for arm in derived.arms}
    arms: list[Arm] = []
    for arm in persisted.arms:
        fresh = unknown_arms.pop(_arm_key(arm), None)
        if fresh is not None and _became_available(arm.condition, fresh.condition):
            arm = arm.model_copy(update={"condition": fresh.condition})
        arms.append(arm)
    return Job(
        problems=persisted.problems
        + tuple(
            problem
            for problem in derived.problems
            if (problem.task.source_dir, problem.name) not in known_problems
        ),
        arms=(*arms, *unknown_arms.values()),
        prompt_template=persisted.prompt_template,
        attempts=persisted.attempts,
        n_concurrent=persisted.n_concurrent,
        environment=persisted.environment,
    )


def _arm_key(arm: Arm) -> tuple[str, Path | None]:
    task = getattr(arm.condition, "task", None)
    return arm.name(), None if task is None else task.source_dir


def _became_available(persisted: LibraryCondition, fresh: LibraryCondition) -> bool:
    return (
        isinstance(persisted, AuthoredArtifact)
        and isinstance(fresh, AuthoredArtifact)
        and persisted.incomplete_reason is not None
        and fresh.incomplete_reason is None
    )
