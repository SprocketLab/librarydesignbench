"""Materialize shared build contexts and per-launch Harbor trial configurations."""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from harbor.constants import MAIN_SERVICE_NAME
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.artifacts import effective_artifact_service
from harbor.models.task.artifacts import normalize_artifact_entries
from harbor.models.task.config import HealthcheckConfig
from harbor.models.task.config import TaskConfig as HarborTaskDefinitionConfig
from harbor.models.trial.config import ServiceVolumeConfig
from harbor.models.trial.config import TaskConfig as HarborTaskConfig
from harbor.models.trial.config import TrialConfig as HarborTrialConfig
from harbor.models.trial.config import VerifierConfig as HarborVerifierConfig

from lib_design_bench.harbor.agents import AGENT_ARTIFACT_FAILURE_EXIT_CODE
from lib_design_bench.harbor.agents import AGENT_ARTIFACT_FAILURE_MARKER
from lib_design_bench.harbor.agents import workspace_setup_agent_config
from lib_design_bench.harbor.languages import language_materialization_policy
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.manifest import DesignLaunch
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.task import LIBRARY_INSTALL
from lib_design_bench.models.task import WORKSPACE_LOCATION
from lib_design_bench.models.task import Task

_MATERIALIZATION_IGNORE_NAMES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "environment",
        "target",
    }
)


def copy_task_tree(source: Path, destination: Path, shared_environment: Path) -> None:
    """Copy Evaluation Phase Harbor task inputs without generated local artifacts."""
    _copy_task_input(source, destination)
    _copy_task_input(shared_environment, destination / "environment")


def _copy_task_input(source: Path, destination: Path) -> None:
    """Copy one task input tree while excluding a nested destination subtree."""
    nested_destination = (
        destination.relative_to(source).parts
        if destination.is_relative_to(source)
        else ()
    )

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(_MATERIALIZATION_IGNORE_NAMES.intersection(names))
        if Path(directory) == source and nested_destination:
            ignored.add(nested_destination[0])
        return ignored

    shutil.copytree(source, destination, ignore=ignore)


def materialize_reference_solution(task_dir: Path, solution_name: str) -> None:
    """Expose one condition-specific reference at Harbor's solution root."""
    solution_root = task_dir / "solution"
    source = solution_root / solution_name
    if not source.is_dir():
        raise ValueError(f"Problem has no solution for {solution_name!r}: {task_dir}")
    selected = task_dir.parent / f".{task_dir.name}-solution"
    if selected.exists():
        shutil.rmtree(selected)
    shutil.copytree(source, selected)
    shutil.rmtree(solution_root)
    shutil.move(selected, solution_root)


# Harbor extracts the collected workspace with tarfile's data filter, which
# rejects any symlink to an absolute path and fails the whole workspace, so the
# trial is unreplayable and the Evaluation Phase cannot build on it. A
# virtualenv's `bin/python` is such a link. The image provisions `.venv` for
# verifier and formatter tooling, and agents build their own under other names
# (for instance `output/wheel-env` to test a wheel), so the interpreter links
# are excluded wherever a venv sits. GNU tar matches an unanchored pattern against
# any trailing run of path components.
WORKSPACE_ARTIFACT_EXCLUDES = (".venv", "bin/python*")


def build_context(launch: TrialLaunch, run_dir: Path) -> Path:
    """Copy and install the build context every launch with this key shares.

    A context holds task, environment, and library-condition material only.
    Which implementor mounts it and on which attempt live on the trial
    configuration, so one directory serves every cell sharing the key.
    """
    task_dir = run_dir / "build-contexts" / launch.build_context_key
    if task_dir.exists():
        shutil.rmtree(task_dir)
    copy_task_tree(launch.task_dir, task_dir, launch.task.environment_dir)
    if isinstance(launch, DesignLaunch):
        _install_design(launch.task, task_dir)
    else:
        _install_condition(launch.task, launch.arm.condition, task_dir)
    if (task_dir / "solution" / launch.library_name).is_dir():
        materialize_reference_solution(task_dir, launch.library_name)
    return task_dir


def trial_config(
    launch: TrialLaunch, *, run_dir: Path, task_dir: Path, trials_dir: Path
) -> HarborTrialConfig:
    """Configure one Harbor trial over an already materialized build context."""
    environment = launch.environment
    library_source = None
    if isinstance(launch, EvaluationLaunch) and isinstance(
        condition := launch.arm.condition, AuthoredArtifact
    ):
        if environment.type in (None, EnvironmentType.DOCKER):
            mount: ServiceVolumeConfig = {
                "type": "bind",
                "source": condition.source.as_posix(),
                "target": LIBRARY_INSTALL,
                "read_only": True,
                "bind": {"create_host_path": False},
            }
            environment = environment.model_copy(
                update={"mounts": [*(environment.mounts or ()), mount]}
            )
        else:
            library_source = condition.source
    agent = launch.agent
    if (prompt_path := launch.prompt_path(run_dir)) is not None:
        agent = agent.model_copy(
            update={
                "kwargs": {**agent.kwargs, "prompt_template_path": str(prompt_path)}
            }
        )
    agent = workspace_setup_agent_config(
        agent,
        task_dir=task_dir,
        trial_dir=trials_dir / launch.trial_name,
        library_source=library_source,
    )
    return HarborTrialConfig(
        task=HarborTaskConfig(path=task_dir, source=launch.task_source),
        trials_dir=trials_dir,
        trial_name=launch.trial_name,
        agent=agent,
        environment=environment,
        verifier=HarborVerifierConfig(
            env=launch.verifier_env if isinstance(launch, EvaluationLaunch) else {}
        ),
    )


def _install_condition(
    task: Task,
    condition: NoLibrary | AuthoredArtifact | ExistingLibrary,
    task_dir: Path,
) -> None:
    """Install one Evaluation Phase library condition into a copied Harbor task."""
    match condition:
        case NoLibrary():
            policy = language_materialization_policy(task.language, task.library_name)
            _write_task_config(
                task_dir,
                healthcheck=policy.blacklist_healthcheck(
                    _blacklisted_identifiers(task)
                ),
            )
        case AuthoredArtifact():
            _install_authored(task, task_dir)
        case ExistingLibrary():
            _install_existing(task, condition, task_dir)


def _install_design(task: Task, task_dir: Path) -> None:
    policy = language_materialization_policy(task.language, task.library_name)
    authored = policy.authored_plan(design=True)
    _install_design_readiness_check(
        task_dir,
        authored.setup_script,
        environment_requirements=(
            task.environment_dir / "requirements.txt"
            if task.language == "python"
            else None
        ),
    )
    _write_task_config(
        task_dir,
        healthcheck=policy.blacklist_healthcheck(_blacklisted_identifiers(task)),
    )


def _install_authored(task: Task, task_dir: Path) -> None:
    policy = language_materialization_policy(task.language, task.library_name)
    authored = policy.authored_plan()
    _install_setup_hook(
        task_dir,
        authored.setup_script,
        preflight_command=authored.preflight_command,
        agent_artifact_failure=True,
    )
    _write_task_config(
        task_dir,
        healthcheck=policy.blacklist_healthcheck(_blacklisted_identifiers(task)),
    )


def _install_existing(
    task: Task,
    condition: ExistingLibrary,
    task_dir: Path,
) -> None:
    library_name = condition.name
    policy = language_materialization_policy(task.language, library_name)
    image_plan = policy.existing_plan(condition.entry, task.environment_dependencies)
    context_dir = task_dir / "environment"
    for name, content in image_plan.files:
        (context_dir / name).write_text(content, encoding="utf-8")
    dockerfile = context_dir / "Dockerfile"
    clone = condition.entry.clone
    ref = condition.entry.ref
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").rstrip() + "\n\n"
        f"RUN rm -rf {LIBRARY_INSTALL} \\\n"
        f"    && mkdir -p {LIBRARY_INSTALL} \\\n"
        f"    && git -C {LIBRARY_INSTALL} init -q \\\n"
        f"    && git -C {LIBRARY_INSTALL} remote add origin {shlex.quote(clone)} \\\n"
        f"    && git -C {LIBRARY_INSTALL} fetch --depth 1 origin {shlex.quote(ref)} \\\n"
        f"    && git -C {LIBRARY_INSTALL} checkout -q FETCH_HEAD\n\n"
        + image_plan.dockerfile_steps,
        encoding="utf-8",
    )
    setup = image_plan.setup_script
    config_dir = condition.entry.config_dir
    if config_dir is not None and (extension := config_dir / "setup.sh").is_file():
        setup += "\n" + extension.read_text(encoding="utf-8")
    _install_setup_hook(task_dir, setup, readiness_command=image_plan.readiness_command)
    _write_task_config(task_dir, healthcheck=None)


def _write_task_config(
    task_dir: Path, *, healthcheck: HealthcheckConfig | None
) -> None:
    """Rewrite the copied task with this launch's Harbor task configuration.

    Every materialized task excludes `WORKSPACE_ARTIFACT_EXCLUDES` from its
    workspace artifact, whatever the checked-in task declares, so no task
    can lose its collected workspace to the image's virtualenv or to one an
    agent built.
    """
    task_config_path = task_dir / "task.toml"
    task = HarborTaskDefinitionConfig.model_validate_toml(
        task_config_path.read_text(encoding="utf-8")
    )
    task.environment.docker_image = None
    task.environment.healthcheck = healthcheck
    task.artifacts = [
        artifact.model_copy(
            update={
                "exclude": [
                    *artifact.exclude,
                    *(
                        pattern
                        for pattern in WORKSPACE_ARTIFACT_EXCLUDES
                        if pattern not in artifact.exclude
                    ),
                ]
            }
        )
        if artifact.source.rstrip("/") == WORKSPACE_LOCATION
        and effective_artifact_service(artifact) == MAIN_SERVICE_NAME
        else artifact
        for artifact in normalize_artifact_entries(task.artifacts)
    ]
    task_config_path.write_text(task.model_dump_toml(), encoding="utf-8")


def _blacklisted_identifiers(task: Task) -> tuple[str, ...]:
    return tuple(
        identifier
        for library in task.existing_libraries.values()
        for identifier in library.isolation_identifiers
    )


def _install_design_readiness_check(
    task_dir: Path, setup_script: str, *, environment_requirements: Path | None
) -> None:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    if environment_requirements is not None:
        shutil.copyfile(
            environment_requirements, tests_dir / "ldb-environment-requirements.txt"
        )
    readiness = tests_dir / "ldb-library-readiness.sh"
    readiness.write_text(setup_script, encoding="utf-8")
    readiness.chmod(0o755)
    verifier = tests_dir / "test.sh"
    verifier.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /logs/verifier\n"
        "if ! /tests/ldb-library-readiness.sh; then\n"
        '  printf \'%s\\n\' \'{"reward": 0.0, "passed": 0, "total": 1}\' > /logs/verifier/reward.json\n'
        "  exit 0\nfi\n"
        'printf \'%s\\n\' \'{"reward": 1.0, "passed": 1, "total": 1}\' > /logs/verifier/reward.json\n',
        encoding="utf-8",
    )
    verifier.chmod(0o755)


def _install_setup_hook(
    task_dir: Path,
    setup_script: str,
    *,
    preflight_command: str = "",
    readiness_command: str = "",
    agent_artifact_failure: bool = False,
) -> None:
    workspace = task_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    selected_setup = workspace / "ldb-library-setup.sh"
    selected_setup.write_text(setup_script, encoding="utf-8")
    selected_setup.chmod(0o755)
    problem_setup = workspace / "setup.sh"
    if problem_setup.is_file():
        problem_setup.replace(workspace / "ldb-problem-setup.sh")
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'setup_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"',
    ]
    if preflight_command:
        lines.append(preflight_command)
    setup_command = (
        f'bash "$setup_dir/ldb-library-setup.sh" {WORKSPACE_LOCATION} {LIBRARY_INSTALL}'
    )
    if agent_artifact_failure:
        lines.extend(
            (
                "set +e",
                setup_command,
                "setup_status=$?",
                "set -e",
                'if [ "$setup_status" -ne 0 ]; then',
                f"  touch {AGENT_ARTIFACT_FAILURE_MARKER}",
                '  rm -f "$setup_dir/ldb-library-setup.sh" "$setup_dir/ldb-problem-setup.sh" "$0"',
                f"  exit {AGENT_ARTIFACT_FAILURE_EXIT_CODE}",
                "fi",
            )
        )
    else:
        lines.append(setup_command)
    if readiness_command:
        lines.append(readiness_command)
    if (workspace / "ldb-problem-setup.sh").is_file():
        lines.append('bash "$setup_dir/ldb-problem-setup.sh"')
    lines.append(
        'rm -f "$setup_dir/ldb-library-setup.sh" "$setup_dir/ldb-problem-setup.sh" "$0"'
    )
    problem_setup.write_text("\n".join(lines) + "\n", encoding="utf-8")
    problem_setup.chmod(0o755)
