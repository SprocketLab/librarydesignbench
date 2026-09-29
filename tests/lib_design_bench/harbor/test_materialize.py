"""Harbor trial configuration produced by LDB materialization."""

from __future__ import annotations

import shutil
from datetime import UTC
from datetime import datetime
from pathlib import Path

from harbor.environments.definition import environment_content_hash
from harbor.environments.definition import has_agent_environment_definition
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.artifacts import normalize_artifact_entries
from harbor.models.task.config import TaskConfig as HarborTaskDefinitionConfig
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import EnvironmentConfig
from harbor.models.trial.config import TrialConfig as HarborTrialConfig

from lib_design_bench.cli.common import live_agent
from lib_design_bench.harbor.materialize import build_context
from lib_design_bench.harbor.materialize import trial_config
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import Job
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.task import LIBRARY_INSTALL
from lib_design_bench.models.task import Task
from lib_design_bench.runs.store import expand

RUN_TIMESTAMP = datetime(2026, 9, 4, 17, 12, 22, tzinfo=UTC)


def test_complete_environment_reaches_harbor(tasks_root: Path, tmp_path: Path) -> None:
    """Equivalent trials provide Harbor the same buildable environment context."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(label="impl", condition=NoLibrary(), agent=AgentConfig(name="oracle")),
        ),
        n_concurrent=1,
    )
    (launch,) = expand(request, run_timestamp=RUN_TIMESTAMP)

    first = _materialize(launch, tmp_path / "first", tmp_path / "first-trials")
    second = _materialize(launch, tmp_path / "second", tmp_path / "second-trials")

    assert first.task.path is not None
    assert second.task.path is not None
    first_environment = first.task.path / "environment"
    second_environment = second.task.path / "environment"
    assert _tree(first_environment) == _tree(second_environment)
    assert environment_content_hash(first_environment) == environment_content_hash(
        second_environment
    )
    assert has_agent_environment_definition(first_environment)
    assert _tree(task.environment_dir).items() <= _tree(first_environment).items()
    assert first.task.path != second.task.path
    first_task = HarborTaskDefinitionConfig.model_validate_toml(
        first.task.path.joinpath("task.toml").read_text(encoding="utf-8")
    )
    assert first_task.environment.docker_image is None


def test_docker_mount_preserves_environment(tasks_root: Path, tmp_path: Path) -> None:
    """The selected Harbor provider and its configuration survive installation."""
    task = Task.from_dir(tasks_root / "pyt")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    environment = EnvironmentConfig(
        type=EnvironmentType.DOCKER, kwargs={"compose_project_name": "trial"}
    )
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(
                label="impl",
                condition=AuthoredArtifact(
                    task=task,
                    source=artifact.resolve(),
                    attempt=1,
                    problems=("01_step",),
                ),
                agent=AgentConfig(name="oracle"),
            ),
        ),
        n_concurrent=1,
        environment=environment,
    )
    (launch,) = expand(request, run_timestamp=RUN_TIMESTAMP)

    config = _materialize(launch, tmp_path / "run", tmp_path / "trials")

    assert config.environment.type is EnvironmentType.DOCKER
    assert config.environment.kwargs == {"compose_project_name": "trial"}
    assert config.environment.mounts is not None
    assert config.environment.mounts[-1]["source"] == artifact.as_posix()
    assert config.environment.mounts[-1]["target"] == LIBRARY_INSTALL
    assert config.environment.mounts[-1]["read_only"] is True


def test_remote_authored_artifact_uses_agent(tasks_root: Path, tmp_path: Path) -> None:
    """Remote providers upload authored artifacts instead of creating host binds."""
    task = Task.from_dir(tasks_root / "pyt")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    kwargs = {"app_name": "ldb-tests", "secrets": ["registry"]}
    environment = EnvironmentConfig(type=EnvironmentType.MODAL, kwargs=kwargs)
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(
                label="impl",
                condition=AuthoredArtifact(
                    task=task,
                    source=artifact.resolve(),
                    attempt=1,
                    problems=("01_step",),
                ),
                agent=AgentConfig(name="oracle"),
            ),
        ),
        n_concurrent=1,
        environment=environment,
    )
    (launch,) = expand(request, run_timestamp=RUN_TIMESTAMP)

    config = _materialize(launch, tmp_path / "run", tmp_path / "trials")

    assert config.environment.type is EnvironmentType.MODAL
    assert config.environment.kwargs == kwargs
    assert config.environment.mounts is None
    assert config.agent.kwargs["library_source"] == artifact.as_posix()


def test_cells_for_different_implementors_share_one_build_context(
    tasks_root: Path, tmp_path: Path
) -> None:
    """Implementors and attempts over one cell prepare a single Harbor task directory."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(
                label="luna",
                condition=NoLibrary(),
                agent=AgentConfig(name="oracle", model_name="luna-1"),
            ),
            Arm(
                label="ds4",
                condition=NoLibrary(),
                agent=AgentConfig(name="oracle", model_name="ds4-1"),
            ),
        ),
        attempts=(1, 2),
        n_concurrent=1,
    )
    launches = expand(request, run_timestamp=RUN_TIMESTAMP)
    run_dir = tmp_path / "run"

    contexts = [build_context(launch, run_dir) for launch in launches]

    assert len(launches) == 4
    assert set(contexts) == {
        run_dir / "build-contexts" / "pyt-evaluation-01_step-no-library"
    }
    assert [path.name for path in (run_dir / "build-contexts").iterdir()] == [
        "pyt-evaluation-01_step-no-library"
    ]


def test_cells_for_different_conditions_get_separate_build_contexts(
    tasks_root: Path, tmp_path: Path
) -> None:
    """One implementor's library conditions never share a prepared Harbor task."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(label="luna", condition=NoLibrary(), agent=AgentConfig(name="oracle")),
            Arm(
                label="luna",
                condition=ExistingLibrary(
                    task=task,
                    name="more-itertools",
                    entry=task.existing_libraries["more-itertools"],
                ),
                agent=AgentConfig(name="oracle"),
            ),
        ),
        n_concurrent=1,
    )
    floor, comparator = expand(request, run_timestamp=RUN_TIMESTAMP)

    floor_context = build_context(floor, run_dir := tmp_path / "run")
    comparator_context = build_context(comparator, run_dir)

    assert floor_context.name == "pyt-evaluation-01_step-no-library"
    assert comparator_context.name == "pyt-evaluation-01_step-more-itertools"
    assert environment_content_hash(
        floor_context / "environment"
    ) != environment_content_hash(comparator_context / "environment")


def test_shared_context_trial_configs_differ_only_in_agent_and_prompt_path(
    tasks_root: Path, tmp_path: Path
) -> None:
    """Sharing a context leaves implementor identity entirely on the trial config."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(
                label="luna",
                condition=NoLibrary(),
                agent=AgentConfig(name="oracle", model_name="luna-1"),
            ),
            Arm(
                label="ds4",
                condition=NoLibrary(),
                agent=AgentConfig(name="oracle", model_name="ds4-1"),
            ),
        ),
        prompt_template="{{instruction}}",
        n_concurrent=1,
    )
    luna, ds4 = expand(request, run_timestamp=RUN_TIMESTAMP)
    run_dir = tmp_path / "run"
    trials_dir = tmp_path / "trials"
    task_dir = build_context(luna, run_dir)

    first = trial_config(
        luna, run_dir=run_dir, task_dir=task_dir, trials_dir=trials_dir
    )
    second = trial_config(
        ds4, run_dir=run_dir, task_dir=task_dir, trials_dir=trials_dir
    )

    assert first.task.path == second.task.path == task_dir
    assert first.model_dump(exclude={"agent", "trial_name"}) == second.model_dump(
        exclude={"agent", "trial_name"}
    )
    assert first.trial_name != second.trial_name
    luna_agent = AgentConfig.model_validate(first.agent.kwargs["inner_agent"])
    ds4_agent = AgentConfig.model_validate(second.agent.kwargs["inner_agent"])
    assert (luna_agent.model_name, ds4_agent.model_name) == ("luna-1", "ds4-1")
    assert luna_agent.kwargs["prompt_template_path"] == str(
        run_dir / "prompts" / "pyt__luna__no-library.md"
    )
    assert ds4_agent.kwargs["prompt_template_path"] == str(
        run_dir / "prompts" / "pyt__ds4__no-library.md"
    )


def _materialize(
    launch: TrialLaunch, run_dir: Path, trials_dir: Path
) -> HarborTrialConfig:
    """Prepare one launch's shared build context and its trial configuration."""
    return trial_config(
        launch,
        run_dir=run_dir,
        task_dir=build_context(launch, run_dir),
        trials_dir=trials_dir,
    )


def _tree(root: Path) -> dict[Path, bytes]:
    """Return the complete copied build context by relative path and bytes."""
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_materialize_excludes_the_workspace_virtualenv_a_task_forgot(
    tasks_root: Path, tmp_path: Path
) -> None:
    """Collection survives the image venv and any venv an agent builds.

    Harbor's data-filtered extraction rejects a venv's absolute `bin/python`
    link and fails the whole workspace, so both the image's `.venv` and the
    interpreter links of a venv under any other name are excluded.
    """
    task_dir = tmp_path / "rsj"
    shutil.copytree(tasks_root / "rsj", task_dir, symlinks=True)
    task_path = task_dir / "evaluation" / "01_step" / "task.toml"
    declared = HarborTaskDefinitionConfig.model_validate_toml(
        task_path.read_text(encoding="utf-8")
    )
    workspace = normalize_artifact_entries(declared.artifacts)[0]
    kept = [pattern for pattern in workspace.exclude if ".venv" not in pattern]
    declared.artifacts = [workspace.model_copy(update={"exclude": kept})]
    task_path.write_text(declared.model_dump_toml(), encoding="utf-8")

    launch = EvaluationLaunch(
        problem=Task.from_dir(task_dir).problem("01_step"),
        arm=Arm(
            label="impl",
            condition=NoLibrary(),
            agent=live_agent(
                "codex",
                "gpt-test",
                reasoning=None,
                agent_version=None,
                agent_kwargs=None,
                env={},
                allowed_hosts=(),
            ),
        ),
        attempt=1,
        run_timestamp=RUN_TIMESTAMP,
    )

    harbor_task_dir = build_context(launch, tmp_path / "run")

    materialized = HarborTaskDefinitionConfig.model_validate_toml(
        (harbor_task_dir / "task.toml").read_text(encoding="utf-8")
    )
    assert tuple(normalize_artifact_entries(materialized.artifacts)[0].exclude) == (
        *kept,
        ".venv",
        "bin/python*",
    )
