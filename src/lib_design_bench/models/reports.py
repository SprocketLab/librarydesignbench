"""Persisted usage, static metrics, trial and run reports, and the LDB result."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal
from typing import Self
from uuid import UUID

from harbor.models.trial.config import AgentConfig
from harbor.models.trial.result import ExceptionInfo
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


StrictNonNegativeFloat = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class UsageReport(BaseModel):
    """Typed usage metrics and optional standardized pricing for one trial.

    `uncached_input_tokens` is the normalized pricing count when a source's
    `input_tokens` includes cache reads. `cost_usd` is the effective amount used
    by reports; repricing preserves its source in
    `reported_cost_usd` and records the replacement in `standardized_cost_usd`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    net_token_usage: StrictNonNegativeInt | None = None
    last_token_usage: StrictNonNegativeInt | None = None
    time_spent: StrictNonNegativeFloat | None = None
    input_tokens: StrictNonNegativeFloat | None = None
    uncached_input_tokens: StrictNonNegativeFloat | None = None
    cache_input_tokens: StrictNonNegativeFloat | None = None
    output_tokens: StrictNonNegativeFloat | None = None
    cost_usd: StrictNonNegativeFloat | None = None
    reported_cost_usd: StrictNonNegativeFloat | None = None
    standardized_cost_usd: StrictNonNegativeFloat | None = None
    input_cost_per_million: StrictNonNegativeFloat | None = None
    output_cost_per_million: StrictNonNegativeFloat | None = None
    cache_input_cost_per_million: StrictNonNegativeFloat | None = None
    agent_steps: StrictNonNegativeFloat | None = None


class SandboxAttempt(BaseModel):
    """One estimated provider charge interval for one physical attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment_type: str = Field(min_length=1)
    execution: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cpus: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    memory_gib: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    storage_gib: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    gpus: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cpu_rate_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    memory_rate_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    storage_rate_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    free_storage_gib: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    pricing_unit: Literal["second", "hour"] | None = None
    estimate_kind: Literal["reserved", "request-estimate", "unknown"]
    scope_complete: bool = True
    pricing_source: str
    exclusions: tuple[str, ...] = ()


class SandboxUsageReport(BaseModel):
    """Persisted external sandbox spend evidence for one logical trial."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    attempts: tuple[SandboxAttempt, ...] = ()


NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


NonNegativeFloat = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class StaticReference(BaseModel):
    """Checked-in static metrics selected as a problem's simplicity reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library: Annotated[str, Field(min_length=1)]
    metrics: StaticMetrics

    @classmethod
    def from_json(cls, path: Path) -> Self:
        """Load a checked-in static reference document."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class StaticMetrics(BaseModel):
    """The six scalar measurements emitted by the verifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stmts: NonNegativeInt
    sloc: NonNegativeInt
    cog_complex: NonNegativeInt
    cyc_complex: NonNegativeInt
    halstead_volume: NonNegativeFloat
    parse_tokens: NonNegativeInt

    def as_scalars(self) -> dict[str, int | float]:
        """Return the complete scalar evidence."""
        return self.model_dump()

    @classmethod
    def scalar_names(cls) -> tuple[str, ...]:
        """Return metric names in persisted order."""
        return tuple(cls.model_fields)


RunReportType = Literal["design", "evaluation"]


LibraryKind = Literal["author", "agent", "existing", "no-library"]


AttemptStatus = Literal["passed", "failed", "completed", "unknown"]


OutcomeClass = Literal["finished", "reanalyze", "reverify", "rerun"]


LimitKind = Literal["cost", "timeout", "none"]


LIMIT_RECORD_NAME = "limit.json"
"""The trial-directory sidecar naming the limit that ended a trial's agent."""


FORMAT_FAILURE_LOG = "format.log"
"""Where a trial's `verifier/` directory records a failed source formatting."""


MEASURE_FAILURE_LOG = "measure.log"
"""Where a trial's `verifier/` directory records a failed static measurement.

The container verifier writes these logs, and static refresh writes the same
names when its own pass fails, so classification can tell a measurement that
was never attempted from one that already failed and would fail again.
"""


AGENT_LIMIT_EXCEPTIONS: dict[str, LimitKind] = {
    # Harbor's installed agents raise this when a provider usage limit is spent.
    "ApiUsageLimitError": "cost",
    # Harbor raises this when the task's own agent timeout expires.
    "AgentTimeoutError": "timeout",
}
"""The Harbor agent-phase exceptions that name a configured limit, not a fault.

The trial completion hook turns these into limit records, and outcome
classification reads them directly for a trial without a limit record.
"""


class LimitRecord(BaseModel):
    """Whether a configured limit ended one trial's agent, and which one.

    Budget exhaustion is not reliably visible in a Harbor result: a spent
    usage limit or agent timeout is only an exception type. The trial
    completion hook records this document instead, so classification and
    reporting read a limit rather than inferring one from `cost_usd` and
    durations.

    One record describes the whole trial, and the last limit its agent phase
    raised wins.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LimitKind
    detail: str = ""

    def write(self, path: Path) -> None:
        """Write this record JSON to `path`, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def from_json(cls, path: Path) -> Self:
        """Load a record from a JSON file."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class TrialIssue(BaseModel):
    """An issue surfaced by one Harbor trial."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trial_name: str
    issue: str
    detail: str
    reward: float | None
    trial_uri: str
    exception_info: ExceptionInfo | None = None


class TrialReport(BaseModel):
    """LDB evidence owned by one Harbor trial attempt.

    `agent`, `model`, and `reasoning` name the implementor that ran this trial.
    One run can cross several implementors, so identity belongs on the trial
    rather than only on the run around it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    trial_name: str = Field(min_length=1)
    implementor: str = Field(min_length=1)
    task: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    library: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    attempt: int | None = None
    design_attempt: int | None = Field(default=None, gt=0)
    source_design_run: str | None = None
    prompt_path: str | None = None
    environment_type: str = Field(min_length=1)
    library_kind: LibraryKind = "no-library"
    trial_path: str = Field(min_length=1)
    trial_uri: str = Field(min_length=1)
    status: AttemptStatus = "unknown"
    reward: float | None = None
    pass_rate: float | None = Field(default=None, ge=0, le=1)
    verifier_rewards: dict[str, float] = Field(default_factory=dict)
    usage: UsageReport = Field(default_factory=UsageReport)
    sandbox_usage: SandboxUsageReport = Field(default_factory=SandboxUsageReport)
    had_error: bool = False
    static_analysis: StaticMetrics | None = None
    incomplete_reason: str | None = None
    reference_library: str | None = None
    simplicity: float | None = 0.0
    score: float = 0.0
    simplicity_ratios: dict[str, float] = Field(default_factory=dict)


class RunReport(BaseModel):
    """Nested persisted results for one benchmark run.

    `agent`, `model`, and `reasoning` describe the run's first launch. That is
    the whole run only for a design run, whose author trials share one agent;
    an evaluation run's per-trial identity lives on each `TrialReport`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    run_type: RunReportType
    repo_commit: str | None = None
    tasks_hash: str | None = None
    agent: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    execution: ExecutionSummary | None = None
    library_attempt_aggregates: tuple[LibraryAttemptAggregate, ...] = ()
    implementor_aggregates: tuple[ImplementorAggregate, ...] = ()
    attempts: tuple[TrialReport, ...] = ()


class Spread(BaseModel):
    """One aggregate, optionally with a conditional repeat confidence interval.

    Score aggregates use equal task weighting and condition-specific
    replicates. Their optional interval fields describe the approximate
    Welch--Satterthwaite 95% interval conditional on the selected benchmark
    and execution protocol. Other retained measures leave these fields absent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mean: float | None
    se: float | None
    n: int = Field(ge=0)
    clusters: int = Field(ge=0)
    df: float | None = None
    ci95_low: float | None = None
    ci95_high: float | None = None
    tasks: int | None = Field(default=None, ge=0)


class ExecutionSummary(BaseModel):
    """The execution settings a result reader needs without opening a sidecar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    started_at: datetime
    n_concurrent: int = Field(gt=0)
    trial_count: int = Field(ge=0)


class ProblemOutcome(BaseModel):
    """One problem's mean outcome within a library-attempt result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(ge=0)
    pass_rate: float | None = Field(default=None, ge=0, le=1)
    simplicity: float | None = None
    simplicity_ratios: dict[str, float] = Field(default_factory=dict)
    mean_cost_usd: float | None = Field(default=None, ge=0)


class LibraryAttemptAggregate(BaseModel):
    """One implementor's outcome with one library on one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    implementor: str = Field(min_length=1)
    task: str = Field(min_length=1)
    library: str = Field(min_length=1)
    design_attempt: int | None = Field(default=None, gt=0)
    score: Spread
    problems: dict[str, ProblemOutcome] = Field(default_factory=dict)


class DesignRow(BaseModel):
    """One design attempt's authoring outcome for one task.

    `cost_usd` is what authoring this library cost, and is absent for a
    provider that reported none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(min_length=1)
    design_attempt: int = Field(gt=0)
    reward: float | None
    outcome: OutcomeClass
    cost_usd: float | None = None


class EvaluationRow(BaseModel):
    """One evaluated cell: an implementor solving one problem with one library.

    `cost_usd` is what this one cell's implementor spent, and is absent for a
    provider that reported none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    implementor: str = Field(min_length=1)
    task: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    design_attempt: int = Field(gt=0)
    evaluation_attempt: int = Field(gt=0)
    pass_rate: float | None = Field(default=None, ge=0, le=1)
    reward: float | None
    simplicity: float | None
    simplicity_ratios: dict[str, float] = Field(default_factory=dict)
    score: float
    outcome: OutcomeClass
    incomplete_reason: str | None
    cost_usd: float | None = None


class ImplementorAggregate(BaseModel):
    """One implementor's aggregate over all its finished Evaluation Phase trials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    implementor: str = Field(min_length=1)
    count: int = Field(ge=0)
    pass_rate: Spread
    reward: Spread
    simplicity: Spread
    score: Spread


class ExperimentResult(BaseModel):
    """The console view of one experiment, rebuilt from both of its runs.

    An experiment is one design run and one evaluation run driven by one
    experiment config. This view carries one row per planned design attempt
    and evaluated cell, and the aggregates a reader is shown. It is never
    persisted; the experiment root's `ldb-result.json` is the saved result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_commit: str = Field(min_length=1)
    tasks_hash: str = Field(min_length=1)
    design_agent: AgentConfig
    implementors: tuple[str, ...]
    design_only: bool
    complete: bool
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    design: tuple[DesignRow, ...] = ()
    evaluation: tuple[EvaluationRow, ...] = ()
    library_attempt_aggregates: tuple[LibraryAttemptAggregate, ...] = ()
    implementor_aggregates: tuple[ImplementorAggregate, ...] = ()


RunReport.model_rebuild()


RESULT_FILE_NAME = "ldb-result.json"
"""Where a run or experiment root keeps its one user-facing result."""


class AgentDetails(BaseModel):
    """Public, non-secret agent selection for a library or implementor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    model: str
    version: str | None = None
    reasoning: str | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)


class ResultMeta(BaseModel):
    """Run provenance and prompt references, not agent or trial evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["design", "evaluation", "replay", "experiment"]
    started_at: datetime
    finished_at: datetime | None = None
    repo_commit: str
    tasks_hash: str
    prompt_paths: tuple[str, ...] = ()
    source_run: str | None = None
    mode: Literal["verify", "remeasure"] | None = None
    complete: bool
    outcome_counts: dict[str, int] = Field(default_factory=dict)


class LibraryResult(BaseModel):
    """One authored, existing, or absent library condition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["authored", "existing", "no-library"]
    task: str
    path: str | None = None
    source_run: str | None = None
    attempt: int | None = None
    author: AgentDetails | None = None
    score: float | None = None
    input_tokens: float | None = None
    output_tokens: float | None = None
    input_cache_tokens: float | None = None
    elapsed: float | None = None
    steps: float | None = None
    cost: float | None = None
    incomplete_reason: str | None = None


class TrialMetric(BaseModel):
    """Compact evaluation observation referencing the two identity registries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    task: str
    attempt: int
    problem: str
    library: str
    implementor: str
    outcome: OutcomeClass
    simplicity: float | None = None
    pass_rate: float | None = None
    score: float | None = None
    input_tokens: float | None = None
    output_tokens: float | None = None
    input_cache_tokens: float | None = None
    elapsed: float | None = None
    steps: float | None = None
    cost: float | None = None
    sandbox_cost: float | None = None
    incomplete_reason: str | None = None


class LdbResult(BaseModel):
    """The LDB result schema, shared by four invocation types."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    id: str
    meta: ResultMeta
    implementors: dict[str, AgentDetails]
    libraries: dict[str, LibraryResult]
    trials: tuple[TrialMetric, ...]

    @model_validator(mode="after")
    def _references_exist(self) -> Self:
        names = [trial.name for trial in self.trials]
        if len(set(names)) != len(names):
            raise ValueError("Trial names must be unique")
        for trial in self.trials:
            library = self.libraries.get(trial.library)
            if library is None:
                raise ValueError(f"Unknown trial library: {trial.library}")
            if library.task != trial.task:
                raise ValueError(f"Trial {trial.name} has a library for another task")
            if trial.implementor not in self.implementors:
                raise ValueError(f"Unknown trial implementor: {trial.implementor}")
        return self

    def to_json(self) -> str:
        """Serialize without inapplicable null fields."""
        return self.model_dump_json(indent=2, exclude_none=True) + "\n"

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load one saved result."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
