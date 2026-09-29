"""LDB's Harbor environment adapters, selected per trial at launch."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import override

import yaml
from harbor.environments.docker.docker import DockerEnvironment
from harbor.environments.modal import ModalEnvironment
from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import TrialConfig
from modal import Sandbox

from lib_design_bench.common import harbor_trial_name

_MODAL_SANDBOX_NAME_MAX_LENGTH = 63


_MODAL_SANDBOX_NAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


_MODAL_SANDBOX_NONCE_BYTES = 4


_sandbox_session_id: ContextVar[str | None] = ContextVar(
    "ldb_modal_sandbox_session_id", default=None
)


def modal_sandbox_name(session_id: str, nonce: str) -> str:
    """Return a Modal sandbox name unique to one creation attempt.

    Modal rejects names of 64 characters or more and refuses to create a
    sandbox whose name already exists in the app. Harbor derives the session
    ID from the trial name, so two processes touching the same trial (a replay
    beside a run, an overlapping resume) would otherwise collide. Keep a
    readable prefix and always append the per-creation nonce; nothing looks a
    sandbox up by name afterwards, Harbor keeps the object ID.
    """
    normalized = _MODAL_SANDBOX_NAME_PATTERN.sub("-", session_id).strip("-._")
    if not normalized:
        normalized = "ldb"
    prefix_length = _MODAL_SANDBOX_NAME_MAX_LENGTH - len(nonce) - 1
    return f"{normalized[:prefix_length].rstrip('-._')}-{nonce}"


class LdbModalEnvironment(ModalEnvironment):
    """Keep Harbor's descriptive session IDs within Modal's sandbox-name limit."""

    @property
    def session_id(self) -> str:
        """Expose the shortened ID only while this task creates a sandbox."""
        return _sandbox_session_id.get() or self._ldb_session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._ldb_session_id = value

    @override
    async def _create_sandbox(
        self,
        *,
        entrypoint: list[str] | None = None,
        block_network: bool | None = None,
        experimental_options: dict[str, Any] | None = None,
    ) -> Sandbox:
        """Call Harbor with a task-local provider name, retaining the trial ID."""
        token = _sandbox_session_id.set(
            modal_sandbox_name(
                self.session_id, secrets.token_hex(_MODAL_SANDBOX_NONCE_BYTES)
            )
        )
        try:
            return await super()._create_sandbox(
                entrypoint=entrypoint,
                block_network=block_network,
                experimental_options=experimental_options,
            )
        finally:
            _sandbox_session_id.reset(token)


MODAL_ENVIRONMENT_IMPORT_PATH = f"{__name__}:{LdbModalEnvironment.__name__}"
"""What every Modal trial runs on, chosen at launch rather than persisted."""


class NamespacedDockerEnvironment(DockerEnvironment):
    """Use LDB's ephemeral Compose project instead of Harbor's trial-derived one."""

    def __init__(self, *, compose_project_name: str, **kwargs: Any) -> None:
        kwargs["session_id"] = compose_project_name
        super().__init__(**kwargs)


_NAMESPACED_DOCKER_ENVIRONMENT = f"{__name__}:{NamespacedDockerEnvironment.__name__}"


def prepare_harbor_trial_configs(
    trial_configs: Sequence[TrialConfig],
) -> tuple[TrialConfig, ...]:
    """Select LDB's environment adapters and reject global task container names.

    Modal trials run on LDB's adapter and Docker trials on an isolated Compose
    project. The choice follows the environment type at launch, so no saved
    request records an adapter's import path.
    """
    return tuple(_prepare_trial_config(config) for config in trial_configs)


def _prepare_trial_config(config: TrialConfig) -> TrialConfig:
    _reject_explicit_container_names(config)
    environment = config.environment
    if environment.type is EnvironmentType.MODAL:
        return config.model_copy(
            update={
                "environment": environment.model_copy(
                    update={"import_path": MODAL_ENVIRONMENT_IMPORT_PATH}
                )
            }
        )
    if environment.import_path == _NAMESPACED_DOCKER_ENVIRONMENT:
        return config
    if environment.import_path is not None:
        return config
    if environment.type not in (None, EnvironmentType.DOCKER):
        return config
    kwargs = dict(environment.kwargs)
    kwargs["compose_project_name"] = compose_project_name(config.trial_name)
    return config.model_copy(
        update={
            "environment": environment.model_copy(
                update={
                    "import_path": _NAMESPACED_DOCKER_ENVIRONMENT,
                    "kwargs": kwargs,
                }
            )
        }
    )


def _reject_explicit_container_names(config: TrialConfig) -> None:
    task_path = config.task.path
    if task_path is None:
        return
    paths = [
        task_path / "environment" / "docker-compose.yaml",
        task_path / "environment" / "docker-compose.yml",
        *config.environment.extra_docker_compose,
    ]
    for path in paths:
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            continue
        for service_name, service in services.items():
            if (
                isinstance(service_name, str)
                and isinstance(service, dict)
                and ("container_name" in service)
            ):
                raise ValueError(
                    f"Compose service {service_name!r} in {path} declares "
                    "`container_name`; LDB requires Compose project-scoped names."
                )


_MAX_COMPOSE_PROJECT_NAME_LENGTH = 58


def compose_project_name(trial_name: str) -> str:
    """Return a bounded, readable Compose project name for one launch."""
    launch_id = f"{datetime.now(UTC):%m%d-%H%M}-{uuid.uuid4().hex[:16]}"
    available_stem_length = (
        _MAX_COMPOSE_PROJECT_NAME_LENGTH - len("ldb--") - len(launch_id)
    )
    safe_trial_name = harbor_trial_name(trial_name)
    if len(safe_trial_name) > available_stem_length:
        digest = hashlib.sha256(trial_name.encode()).hexdigest()[:12]
        prefix_length = available_stem_length - len(digest) - 1
        safe_trial_name = f"{safe_trial_name[:prefix_length].rstrip('-_')}-{digest}"
    return f"ldb-{safe_trial_name}-{launch_id}"
