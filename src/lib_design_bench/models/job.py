"""Frozen requests and arms expanded into planned Harbor trials."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from typing import Literal

from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import EnvironmentConfig
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from lib_design_bench.common import safe_path_part
from lib_design_bench.models.conditions import LibraryCondition
from lib_design_bench.models.conditions import condition_applies
from lib_design_bench.models.conditions import condition_name
from lib_design_bench.models.task import Problem
from lib_design_bench.models.task import Task


def default_environment() -> EnvironmentConfig:
    """Return Docker as the durable execution-environment default."""
    return EnvironmentConfig(type=EnvironmentType.DOCKER)


class Arm(BaseModel):
    """One implementor label, library condition, and agent configuration.

    The label names the implementor that mounts the condition. Two implementors
    evaluating one condition are two arms, so the label is what keeps their
    trial names and run slots apart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    condition: LibraryCondition
    agent: AgentConfig

    @field_validator("label")
    @classmethod
    def _require_path_safe_label(cls, value: str) -> str:
        if value != safe_path_part(value):
            raise ValueError(f"Arm label must be path-safe: {value!r}")
        return value

    def name(self) -> str:
        """Return this arm's stable implementor-scoped condition name."""
        return f"{self.label}__{condition_name(self.condition)}"


def arm_label(agent: AgentConfig) -> str:
    """Return the path-safe label naming a single implementor by its agent and model."""
    return safe_path_part(f"{agent.name or agent.import_path}__{agent.model_name}")


class Job(BaseModel):
    """An Evaluation Phase request over concrete problems and experimental arms."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["job"] = "job"
    problems: tuple[Problem, ...]
    arms: tuple[Arm, ...]
    prompt_template: str | None = None
    attempts: tuple[int, ...] = (1,)
    n_concurrent: int = Field(gt=0)
    environment: EnvironmentConfig = Field(default_factory=default_environment)
    verifier_env: dict[str, str] = Field(default_factory=dict)
    """Environment every trial's `test.sh` runs with, e.g. `LDB_SKIP_TESTS`."""

    @model_validator(mode="after")
    def _validate_job(self) -> Job:
        if not self.problems:
            raise ValueError("Job requires at least one problem")
        problem_keys = tuple(
            (problem.task.source_dir, problem.name) for problem in self.problems
        )
        if len(set(problem_keys)) != len(problem_keys):
            raise ValueError("Job problems must be unique")
        if not self.arms:
            raise ValueError("Job requires at least one arm")
        implementors: dict[str, AgentConfig] = {}
        for arm in self.arms:
            previous = implementors.setdefault(arm.label, arm.agent)
            if previous != arm.agent:
                raise ValueError(
                    f"Implementor {arm.label!r} has conflicting agent configs"
                )
        for problem in self.problems:
            arm_names = tuple(
                arm.name()
                for arm in self.arms
                if condition_applies(arm.condition, problem)
            )
            if len(set(arm_names)) != len(arm_names):
                raise ValueError(
                    "Job arms must be unique per problem by label and condition"
                )
        _validate_attempts(self.attempts, "Job attempts")
        return self

    @property
    def tasks(self) -> tuple[Task, ...]:
        """Return request tasks once, ordered by their names."""
        tasks = {problem.task.source_dir: problem.task for problem in self.problems}
        return tuple(sorted(tasks.values(), key=lambda task: task.name))


class AuthorJob(BaseModel):
    """A Design Phase authoring request over benchmark tasks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["author"] = "author"
    tasks: tuple[Task, ...]
    agent: AgentConfig
    prompt_template: str | None = None
    attempts: tuple[int, ...] = (1,)
    n_concurrent: int = Field(gt=0)
    environment: EnvironmentConfig = Field(default_factory=default_environment)

    @model_validator(mode="after")
    def _validate_author_job(self) -> AuthorJob:
        if not self.tasks:
            raise ValueError("AuthorJob requires at least one task")
        source_dirs = tuple(task.source_dir for task in self.tasks)
        if len(set(source_dirs)) != len(source_dirs):
            raise ValueError("AuthorJob tasks must be unique")
        _validate_attempts(self.attempts, "AuthorJob attempts")
        return self


class ReplayJob(BaseModel):
    """A persisted-run replay request with user-provided name filters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["replay"] = "replay"
    source: Path
    tasks: tuple[str, ...] = ()
    trials: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    task_override: Path | None = None
    n_concurrent: int = Field(gt=0)
    environment: EnvironmentConfig = Field(default_factory=default_environment)
    skip_tests: bool = False
    """Remeasure only: `test.sh` skips the behavioral tests and scores each
    trial with the passed/total counts its source slot recorded."""

    @model_validator(mode="after")
    def _require_absolute_paths(self) -> ReplayJob:
        if not self.source.is_absolute():
            raise ValueError(f"Replay source must be absolute: {self.source}")
        if self.task_override is not None and not self.task_override.is_absolute():
            raise ValueError(
                f"Replay task override must be absolute: {self.task_override}"
            )
        return self


Request = Annotated[Job | AuthorJob | ReplayJob, Field(discriminator="kind")]


def _validate_attempts(attempts: tuple[int, ...], label: str) -> None:
    if not attempts or any(attempt <= 0 for attempt in attempts):
        raise ValueError(f"{label} must be non-empty positive integers")
    if len(set(attempts)) != len(attempts):
        raise ValueError(f"{label} must be unique")
