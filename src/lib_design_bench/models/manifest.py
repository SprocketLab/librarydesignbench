"""Frozen run metadata persisted alongside each run."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing import Literal
from typing import Self

from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import EnvironmentConfig
from harbor.models.trial.config import SourceTrialConfig
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from lib_design_bench.common import evaluation_trial_name
from lib_design_bench.common import harbor_trial_name
from lib_design_bench.common import safe_path_part
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import condition_name
from lib_design_bench.models.experiment import ExperimentConfig
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import Request
from lib_design_bench.models.job import default_environment
from lib_design_bench.models.reports import StaticReference
from lib_design_bench.models.task import Problem
from lib_design_bench.models.task import Task


class DesignLaunch(BaseModel):
    """One planned Design Phase author trial."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["design"] = "design"
    task: Task
    agent: AgentConfig
    prompt_template: str | None
    attempt: int = Field(gt=0)
    task_override: Path | None = None
    environment: EnvironmentConfig = Field(default_factory=default_environment)

    @property
    def task_dir(self) -> Path:
        """Return this launch's checked-in Design Phase Harbor task directory."""
        return (
            self.task_override
            if self.task_override is not None
            else self.task.design_dir
        )

    @property
    def task_source(self) -> str:
        """Return this launch's Harbor task source."""
        return f"{self.task.name}/design"

    @property
    def library_name(self) -> str:
        """Return the library an author trial works under, which is itself."""
        return "author"

    @property
    def trial_name(self) -> str:
        """Return this launch's stable Harbor trial name."""
        return harbor_trial_name(
            f"{safe_path_part(self.task.name)}__phase-1__author__a{self.attempt}"
        )

    @property
    def build_context_key(self) -> str:
        """Return the key of the build context this launch shares.

        Every author attempt on one task prepares the same Harbor task, so the
        attempt stays out of the key.
        """
        return safe_path_part(self.task_source)

    def prompt_relpath(self) -> Path | None:
        """Return the run-relative path of this launch's rendered prompt."""
        if self.prompt_template is None:
            return None
        return Path("prompts") / f"{safe_path_part(self.task.name)}__author.md"

    def prompt_path(self, run_dir: Path) -> Path | None:
        """Return this launch's rendered prompt file when it has a template."""
        relative = self.prompt_relpath()
        return None if relative is None else run_dir / relative


class EvaluationLaunch(BaseModel):
    """One planned Evaluation Phase implementor trial."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["evaluation"] = "evaluation"
    problem: Problem
    arm: Arm
    prompt_template: str | None = None
    attempt: int = Field(gt=0)
    run_timestamp: datetime
    trial_name_override: str | None = Field(default=None, min_length=1)
    task_override: Path | None = None
    environment: EnvironmentConfig = Field(default_factory=default_environment)
    verifier_env: dict[str, str] = Field(default_factory=dict)
    """Environment the task's `test.sh` runs with, e.g. `LDB_SKIP_TESTS`."""

    @property
    def task(self) -> Task:
        """Return the problem's parent task."""
        return self.problem.task

    @property
    def agent(self) -> AgentConfig:
        """Return the finished agent configuration for this implementor trial."""
        return self.arm.agent

    @property
    def task_dir(self) -> Path:
        """Return this launch's checked-in Evaluation Phase Harbor task directory."""
        return (
            self.task_override if self.task_override is not None else self.problem.dir
        )

    @property
    def task_source(self) -> str:
        """Return this launch's Harbor task source."""
        return f"{self.task.name}/{self.problem.harbor_task}"

    @property
    def arm_name(self) -> str:
        """Return the stable implementor-scoped arm name."""
        return self.arm.name()

    @property
    def library_name(self) -> str:
        """Return the mounted library condition, without the implementor label.

        Checked-in reference solutions and retained reports are keyed on the
        library an implementor had, not on which implementor had it.
        """
        return condition_name(self.arm.condition)

    @property
    def trial_name(self) -> str:
        """Return this launch's stable Harbor trial name."""
        if self.trial_name_override is not None:
            return self.trial_name_override
        return evaluation_trial_name(
            task=self.task.name,
            setup=self.library_name,
            problem=self.problem.name,
            attempt=self.attempt,
            agent_name=self.agent.name or self.agent.import_path or "agent",
            implementor_label=self.arm.label,
            run_timestamp=self.run_timestamp,
        )

    @property
    def build_context_key(self) -> str:
        """Return the key of the build context this launch shares.

        A prepared Harbor task carries the problem and its library condition, never
        the implementor that mounts it, so every implementor label and attempt
        over one cell reads the same directory.
        """
        return safe_path_part(f"{self.task_source}/{self.library_name}")

    def prompt_relpath(self) -> Path | None:
        """Return the run-relative path of this launch's rendered prompt."""
        if self.prompt_template is None:
            return None
        return Path("prompts") / f"{safe_path_part(self.task.name)}__{self.arm_name}.md"

    def prompt_path(self, run_dir: Path) -> Path | None:
        """Return this launch's rendered prompt file when it has a template."""
        relative = self.prompt_relpath()
        return None if relative is None else run_dir / relative

    def static_reference(self) -> StaticReference:
        """Require the reference that the Evaluation Phase verifier scores against."""
        reference = self.problem.static_reference()
        if reference is None:
            raise ValueError(f"Missing static reference for {self.problem.dir}")
        return reference

    @property
    def incomplete_reason(self) -> str | None:
        """Return unavailable-library evidence for an authored arm."""
        condition = self.arm.condition
        if isinstance(condition, AuthoredArtifact):
            return condition.incomplete_reason
        return None


TrialLaunch = Annotated[
    DesignLaunch | EvaluationLaunch,
    Field(discriminator="kind"),
]


class ExperimentRecord(BaseModel):
    """The published setup one experiment run was launched from.

    Holds the resolved configuration the run used. A config-form `--task`
    selection is resolved into `config.tasks`; problem and implementor
    filters remain invocation-local.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overrides: tuple[str, ...] = ()
    config: ExperimentConfig
    design_agent: AgentConfig


class RunManifest(BaseModel):
    """The self-describing persisted request and provenance for one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_commit: str
    tasks_hash: str
    timestamp: datetime
    request: Request
    experiment: ExperimentRecord | None = None
    verification: VerificationRecord | None = None

    def write(self, path: Path) -> None:
        """Write this manifest JSON, creating its parent directory."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load a persisted manifest."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class VerificationRecord(BaseModel):
    """Provenance for the verifier results currently stored on a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    repo_commit: str = Field(min_length=1)
    tasks_hash: str = Field(min_length=1)
    source_trials: tuple[SourceTrialConfig, ...] = ()
    tasks: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


RunManifest.model_rebuild()
