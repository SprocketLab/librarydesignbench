"""Experiment configuration: the published setup one `ldb run` executes."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Literal

import yaml
from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import AgentConfig
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from lib_design_bench.common import safe_path_part

# An implementor entry names a Harbor agent, which Harbor resolves when the
# trial starts; `import_path` is not a key.
_SELECTION_FIELDS = frozenset({"name", "model_name", "kwargs"})
_EXECUTION_ONLY_FIELDS = (
    frozenset(AgentConfig.model_fields) - _SELECTION_FIELDS - {"import_path"}
)


_CREDENTIAL_KWARGS = frozenset(
    {
        "api_key",
        "token",
        "jwt_token",
        "password",
        "daytona_api_key",
        "daytona_jwt_token",
        "modal_token_id",
        "modal_token_secret",
        "token_id",
        "token_secret",
    }
)
_SANDBOX_KWARGS = frozenset(
    {"override_cpus", "override_memory_mb", "override_storage_mb"}
)


@dataclass(frozen=True)
class ConfigOverride:
    """One `KEY=VALUE` config override parsed from the command line.

    `path` is the dotted key from the config root, so
    `evaluation.agents.luna.kwargs.reasoning_effort=high` sets that nested key
    to the YAML scalar `high`. `text` is the argument exactly as it was typed,
    which the manifest records.
    """

    text: str
    path: tuple[str, ...]
    value: Any


class Sandbox(BaseModel):
    """Sandbox size one experiment phase requests from its environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=1)
    storage_mb: int = Field(ge=1)


class EnvironmentSettings(BaseModel):
    """Harbor execution provider every trial of a run starts in.

    Validation errors never echo their input, which may hold a credential.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    type: EnvironmentType = EnvironmentType.DOCKER
    kwargs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kwargs")
    @classmethod
    def _reject_credentials_and_sizes(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Keep secrets in the provider's own variables and sizes in `sandbox`."""
        if any(name.lower() in _CREDENTIAL_KWARGS for name in value):
            raise ValueError(
                "Provider credentials must come from the provider's native "
                "environment variables, not `environment.kwargs`."
            )
        if any(name in _SANDBOX_KWARGS for name in value):
            raise ValueError(
                "Sandbox sizes belong in the config's `sandbox` settings, not "
                "`environment.kwargs`."
            )
        return value


class DesignSettings(BaseModel):
    """Design Phase attempts, optional inlined prompt template, and sandbox size.

    A missing prompt means the agent receives the task's raw instruction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempts: int = Field(gt=0)
    prompt: str | None = Field(default=None, min_length=1)
    sandbox: Sandbox


class EvaluationSettings(BaseModel):
    """Implementor agents, attempts, optional prompt template, and sandbox size.

    A missing prompt means every implementor receives the raw instruction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agents: dict[str, AgentConfig] = Field(min_length=1)
    attempts: int = Field(gt=0)
    prompt: str | None = Field(default=None, min_length=1)
    sandbox: Sandbox

    @field_validator("agents")
    @classmethod
    def _validate_implementor_keys(
        cls, value: dict[str, AgentConfig]
    ) -> dict[str, AgentConfig]:
        """Reject keys that cannot appear verbatim in a cell name or slot path."""
        unsafe = tuple(key for key in value if key != safe_path_part(key))
        if unsafe:
            raise ValueError(
                "Implementor keys must be path-safe: "
                f"{', '.join(repr(key) for key in unsafe)}"
            )
        return value


class ExperimentConfig(BaseModel):
    """One Design Phase and one Evaluation Phase over the selected tasks.

    Validation errors never echo their input, which may hold a credential.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    version: Literal["1.0.0"]
    tasks: tuple[str, ...] = ()
    tasks_root: Path | None = None
    environment: EnvironmentSettings = Field(default_factory=EnvironmentSettings)
    design: DesignSettings
    evaluation: EvaluationSettings


def load_experiment_config(
    path: Path,
    *,
    overrides: Sequence[ConfigOverride],
    agent_env: Mapping[str, str],
    allowed_hosts: Sequence[str],
) -> ExperimentConfig:
    """Load one experiment config, inlining prompts and resolving implementors.

    Overrides land on the raw document before anything is resolved, so an
    overridden value faces the same rules as a written one and the returned
    config states what actually ran. Every implementor gets `agent_env` and
    `allowed_hosts`, which come from the command line rather than the file.
    """
    config_path = path.expanduser().resolve()
    config_text = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(config_text)
    if not isinstance(data, dict):
        raise ValueError(f"Experiment config must be a mapping: {config_path}")
    apply_overrides(data, overrides)
    for phase in ("design", "evaluation"):
        settings = data.get(phase)
        if not isinstance(settings, dict):
            raise ValueError(
                f"Experiment config `{phase}` must be a mapping: {config_path}"
            )
        if "prompt" in settings:
            settings["prompt"] = _prompt_text(config_path.parent, settings["prompt"])
    entries = data["evaluation"].get("agents")
    if not isinstance(entries, dict):
        raise ValueError(
            f"Experiment config `evaluation.agents` must be a mapping: {config_path}"
        )
    agents: dict[str, dict[str, Any]] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Implementor {key!r} must be a mapping: {entry!r}")
        agents[key] = entry
    data["evaluation"]["agents"] = {
        key: _implementor_agent(key, entry, agent_env, allowed_hosts)
        for key, entry in agents.items()
    }
    return ExperimentConfig.model_validate(data)


def apply_overrides(data: dict[str, Any], overrides: Sequence[ConfigOverride]) -> None:
    """Set each override's dotted key on a raw config document, in place."""
    for override in overrides:
        target = data
        for depth, segment in enumerate(override.path[:-1]):
            child = target.setdefault(segment, {})
            if not isinstance(child, dict):
                reached = ".".join(override.path[: depth + 1])
                raise ValueError(
                    f"Override {override.text!r} cannot descend into `{reached}`, "
                    f"which holds {child!r} rather than a mapping."
                )
            target = child
        target[override.path[-1]] = override.value


def _prompt_text(config_dir: Path, value: Any) -> str:
    """Read one prompt template named relative to the config file."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Experiment prompt must be a path to a template file: {value!r}"
        )
    prompt_path = (config_dir / value.strip()).resolve()
    if not prompt_path.is_file():
        raise ValueError(f"Experiment prompt file does not exist: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _implementor_agent(
    key: str,
    entry: dict[str, Any],
    env: Mapping[str, str],
    allowed_hosts: Sequence[str],
) -> AgentConfig:
    """Resolve one implementor entry into a complete Harbor agent configuration."""
    execution_only = sorted(_EXECUTION_ONLY_FIELDS.intersection(entry))
    if execution_only:
        raise ValueError(
            f"Implementor {key!r} sets execution-only field(s): "
            f"{', '.join(execution_only)}. Agent environment and hosts come "
            "from the command line, not from the experiment config."
        )
    unknown = sorted(set(entry) - _SELECTION_FIELDS)
    if unknown:
        raise ValueError(
            f"Implementor {key!r} has unknown field(s): {', '.join(unknown)}. "
            f"Supported fields: {', '.join(sorted(_SELECTION_FIELDS))}."
        )
    kwargs = entry.get("kwargs", {})
    if not isinstance(kwargs, dict):
        raise ValueError(f"Implementor {key!r} kwargs must be a mapping: {kwargs!r}")
    return AgentConfig(
        name=_implementor_string(key, entry, "name"),
        model_name=_implementor_string(key, entry, "model_name"),
        kwargs=dict(kwargs),
        env=dict(env),
        extra_allowed_hosts=list(allowed_hosts),
    )


def _implementor_string(key: str, entry: dict[str, Any], field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Implementor {key!r} requires a non-empty `{field}`: {value!r}"
        )
    return value.strip()
