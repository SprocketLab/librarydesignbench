"""Frozen library conditions available to Evaluation Phase implementors."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator
from structlog import get_logger

from lib_design_bench.common import safe_path_part
from lib_design_bench.models.task import ExistingLibraryEntry
from lib_design_bench.models.task import Problem
from lib_design_bench.models.task import Task

logger = get_logger(__name__)


class NoLibrary(BaseModel):
    """The no-library floor condition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["no-library"] = "no-library"
    problems: tuple[str, ...] = ()

    @field_validator("problems")
    @classmethod
    def _validate_problems(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not problem.strip() for problem in value):
            raise ValueError("no-library scope problems must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("no-library scope problems must be unique")
        return value


class AuthoredArtifact(BaseModel):
    """A mounted artifact produced by a Design Phase author attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["authored"] = "authored"
    task: Task
    source: Path
    attempt: int = Field(gt=0)
    problems: tuple[str, ...]
    incomplete_reason: str | None = None

    @field_validator("source")
    @classmethod
    def _require_absolute_source(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"authored artifact source must be absolute: {value}")
        return value

    @field_validator("problems")
    @classmethod
    def _require_problems(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("authored artifact problems must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("authored artifact problems must be unique")
        return value

    @field_validator("incomplete_reason")
    @classmethod
    def _normalize_incomplete_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("authored artifact incomplete_reason must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_source_and_scope(self) -> AuthoredArtifact:
        unknown = set(self.problems) - set(self.task.problems)
        if unknown:
            raise ValueError(
                "authored artifact problems are not active for "
                f"{self.task.name!r}: {sorted(unknown)}"
            )
        if not self.source.exists() and self.incomplete_reason is None:
            raise ValueError(f"authored artifact source does not exist: {self.source}")
        return self


class ExistingLibrary(BaseModel):
    """A pinned production-library comparison condition for one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["existing"] = "existing"
    task: Task
    name: str
    entry: ExistingLibraryEntry
    problems: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_declared_entry(self) -> ExistingLibrary:
        declared_entry = self.task.existing_libraries.get(self.name)
        if declared_entry is None:
            raise ValueError(
                f"existing library {self.name!r} is not declared by task "
                f"{self.task.name!r}"
            )
        if self.entry != declared_entry:
            logger.warning(
                "Existing library entry differs from the task's current "
                "declaration; keeping the saved entry",
                library=self.name,
                task=self.task.name,
            )
        unknown_problems = set(self.problems) - set(self.task.problems)
        if unknown_problems:
            raise ValueError(
                f"existing library {self.name!r} scopes unknown problems for "
                f"task {self.task.name!r}: {sorted(unknown_problems)}"
            )
        if len(set(self.problems)) != len(self.problems):
            raise ValueError(
                f"existing library {self.name!r} scopes duplicate problems"
            )
        return self


LibraryCondition = Annotated[
    NoLibrary | AuthoredArtifact | ExistingLibrary,
    Field(discriminator="kind"),
]


def condition_name(condition: LibraryCondition) -> str:
    """Return the stable path-safe arm name for a library condition."""
    if isinstance(condition, NoLibrary):
        return "no-library"
    if isinstance(condition, AuthoredArtifact):
        return f"a{condition.attempt}"
    return safe_path_part(condition.name)


def condition_applies(condition: LibraryCondition, problem: Problem) -> bool:
    """Return whether a library condition may execute for this problem."""
    match condition:
        case ExistingLibrary():
            return condition.task.source_dir == problem.task.source_dir and (
                not condition.problems or problem.name in condition.problems
            )
        case AuthoredArtifact():
            if condition.task.source_dir != problem.task.source_dir:
                return False
            return problem.name in condition.problems
        case NoLibrary():
            return not condition.problems or problem.name in condition.problems
