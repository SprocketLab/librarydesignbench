"""A persisted run directory: its planned launches, slots, and output documents."""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Self
from uuid import UUID

import structlog
from harbor.constants import MAIN_SERVICE_NAME
from harbor.models.job.result import JobResult
from harbor.models.job.result import JobStats
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult
from harbor.trial.regrade import RegradeError
from harbor.trial.regrade import read_artifact_manifest
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.conditions import condition_applies
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import ReplayJob
from lib_design_bench.models.job import Request
from lib_design_bench.models.manifest import DesignLaunch
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import RunManifest
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import LIMIT_RECORD_NAME
from lib_design_bench.models.reports import RESULT_FILE_NAME
from lib_design_bench.models.reports import ExecutionSummary
from lib_design_bench.models.reports import LimitRecord
from lib_design_bench.models.reports import RunReport
from lib_design_bench.models.reports import RunReportType
from lib_design_bench.models.reports import SandboxUsageReport
from lib_design_bench.models.reports import TrialReport
from lib_design_bench.models.task import WORKSPACE_LOCATION

logger = structlog.get_logger(__name__)


DESIGN_RESULTS_DIR_NAME = "design_results"
"""The design run subdirectory holding one author slot per planned attempt."""


EVALUATION_RESULTS_DIR_NAME = "evaluation_results"
"""The design run subdirectory holding its one evaluation run."""


MANIFEST_FILE_NAME = "manifest.json"
"""The run root's manifest, whose request is the authority on what it runs."""

TRIAL_RESULT_FILE_NAME = "result.json"
"""A slot's Harbor trial result."""

TRIAL_REPORT_FILE_NAME = "trial-report.json"
"""A slot's LDB trial report, which holds its pricing policy."""

SANDBOX_LEDGER_DIR_NAME = "sandbox-usage"
"""The run root's per-trial sandbox spend, which outlives a slot being cleared."""


@dataclass(frozen=True)
class Slot:
    """One durable trial location within a typed run."""

    dir: Path

    def result(self) -> TrialResult | None:
        """Return a valid matching Harbor result, if one has been persisted."""
        path = self.dir / TRIAL_RESULT_FILE_NAME
        result = _read_optional(
            path,
            lambda document: TrialResult.model_validate_json(
                document.read_text(encoding="utf-8")
            ),
        )
        if result is None:
            return None
        if result.trial_name != self.dir.name:
            logger.warning(
                "Ignoring inconsistent persisted document",
                path=path.as_posix(),
                expected_trial_name=self.dir.name,
                trial_name=result.trial_name,
            )
            return None
        return result

    def reward(self) -> float | None:
        """Return this slot's verifier reward, if Harbor recorded one."""
        return verifier_reward(self.result())

    def behavioral_counts(self) -> tuple[int, int] | None:
        """Return the passed/total counts this slot's verifier recorded, if valid."""
        rewards = verifier_rewards(self.result())
        passed, total = rewards.get("passed"), rewards.get("total")
        if isinstance(passed, int) and isinstance(total, int) and 0 <= passed <= total:
            return passed, total
        return None

    def workspace_error(self) -> str | None:
        """Return why this slot holds no collected workspace directory, if it does not."""
        try:
            workspace = workspace_artifact(self.dir)
        except (RegradeError, ValueError) as error:
            return str(error)
        return (
            None if workspace.is_dir() else f"missing workspace artifact: {workspace}"
        )

    def limit(self) -> LimitRecord | None:
        """Return the persisted limit that ended this trial's agent, if written."""
        return _read_optional(self.dir / LIMIT_RECORD_NAME, LimitRecord.from_json)

    def trial_report(self) -> TrialReport | None:
        """Return this slot's LDB trial report, failing on a corrupt one.

        The report holds the trial's pricing policy, so a corrupt one must be
        repaired rather than silently rebuilt without it.
        """
        path = self.dir / TRIAL_REPORT_FILE_NAME
        if not path.is_file():
            return None
        return TrialReport.model_validate_json(path.read_text(encoding="utf-8"))

    def clear(self) -> None:
        """Remove this trial slot if it exists."""
        if self.dir.exists():
            shutil.rmtree(self.dir)


@dataclass(frozen=True)
class Run:
    """A run directory with its manifest loaded once."""

    dir: Path
    manifest: RunManifest

    @classmethod
    def create(cls, dir: Path, manifest: RunManifest) -> Run:
        """Create the required physical layout and persist one typed run."""
        _create_layout(dir, manifest.request)
        manifest.write(dir / MANIFEST_FILE_NAME)
        return cls(dir=dir, manifest=manifest)

    @classmethod
    def open(cls, dir: Path) -> Run:
        """Open and verify a typed run directory."""
        manifest_path = dir / MANIFEST_FILE_NAME
        if not manifest_path.is_file():
            raise FileNotFoundError(manifest_path)
        manifest = RunManifest.load(manifest_path)
        _validate_layout(dir, manifest.request)
        return cls(dir=dir, manifest=manifest)

    @classmethod
    def exists(cls, dir: Path) -> bool:
        """Return whether a typed manifest exists in this directory."""
        return (dir / MANIFEST_FILE_NAME).is_file()

    def request(self) -> Request:
        """Return this run's persisted request."""
        return self.manifest.request

    def evaluation_child(self) -> Run | None:
        """Return the evaluation run an experiment's design run holds, once planned."""
        child = self.dir / EVALUATION_RESULTS_DIR_NAME
        return Run.open(child) if Run.exists(child) else None

    def result_owner(self) -> Run:
        """Return the run holding this invocation's one user-facing result.

        An experiment's evaluation run publishes through its design run.
        """
        parent = self.dir.parent
        if self.dir.name != EVALUATION_RESULTS_DIR_NAME or not Run.exists(parent):
            return self
        return Run.open(parent)

    def unpublish_result(self) -> None:
        """Remove the published result before the evidence beneath it changes."""
        (self.result_owner().dir / RESULT_FILE_NAME).unlink(missing_ok=True)

    def launches(self) -> tuple[TrialLaunch, ...]:
        """Return this run's deterministically expanded planned trials."""

        return expand(self.request(), run_timestamp=self.manifest.timestamp)

    def slot(self, launch: TrialLaunch) -> Slot:
        """Return the durable slot for one launch belonging to this run."""
        request = self.request()
        if isinstance(request, AuthorJob) and not isinstance(launch, DesignLaunch):
            raise ValueError("Design runs only contain Design Phase launches")
        if isinstance(request, Job) and not isinstance(launch, EvaluationLaunch):
            raise ValueError("Evaluation runs only contain Evaluation Phase launches")
        return Slot(dir=slot_path(self.dir, request, launch.trial_name))


def verifier_rewards(result: TrialResult | None) -> dict[str, Any]:
    """Return the rewards Harbor's verifier recorded, empty when it recorded none."""
    verifier = None if result is None else result.verifier_result
    return (None if verifier is None else verifier.rewards) or {}


def verifier_reward(result: TrialResult | None) -> float | None:
    """Return the primary reward from a Harbor trial's verifier result."""
    reward = verifier_rewards(result).get("reward")
    return None if reward is None else float(reward)


def workspace_artifact(trial_dir: Path) -> Path:
    """Resolve the workspace that Harbor recorded as successfully collected."""
    entries = read_artifact_manifest(trial_dir)
    for entry in reversed(entries):
        if (
            entry.source.rstrip("/") != WORKSPACE_LOCATION
            or (entry.service or MAIN_SERVICE_NAME) != MAIN_SERVICE_NAME
        ):
            continue
        if entry.status not in ("ok", "empty") or entry.type != "directory":
            raise ValueError(
                f"Harbor did not collect a workspace directory for {trial_dir.name}: "
                f"status={entry.status}, type={entry.type}"
            )
        return trial_dir / entry.destination
    raise ValueError(f"Harbor recorded no workspace artifact for {trial_dir.name}")


def sandbox_ledger_path(run_dir: Path, trial_name: str) -> Path:
    """Return where one trial's sandbox spend is recorded at its run root."""
    return run_dir / SANDBOX_LEDGER_DIR_NAME / f"{trial_name}.json"


def slot_path(run_dir: Path, request: Request, trial_name: str) -> Path:
    """Resolve a persisted trial name through the Design/Evaluation slot layout."""
    return slot_dir(
        run_dir,
        "design" if isinstance(request, AuthorJob) else "evaluation",
        trial_name,
    )


def slot_dir(run_dir: Path, run_type: RunReportType, trial_name: str) -> Path:
    """Resolve a persisted trial name from the run type its report records.

    The layout depends only on whether the run authored libraries, so a reader
    holding a `RunReport` resolves slots without reopening the manifest.
    """
    if run_type == "design":
        return run_dir / DESIGN_RESULTS_DIR_NAME / trial_name
    return run_dir / trial_name


def _create_layout(dir: Path, request: Request) -> None:
    dir.mkdir(parents=True, exist_ok=True)
    if isinstance(request, AuthorJob):
        (dir / DESIGN_RESULTS_DIR_NAME).mkdir(exist_ok=True)


def _validate_layout(dir: Path, request: Request) -> None:
    if not isinstance(request, AuthorJob):
        return
    if not (dir / DESIGN_RESULTS_DIR_NAME).is_dir():
        raise ValueError(
            f"Design run layout is missing: {DESIGN_RESULTS_DIR_NAME} in {dir}"
        )


def _read_optional[T](path: Path, reader: Callable[[Path], T]) -> T | None:
    """Read an optional persisted document, warning only when it is malformed."""
    if not path.is_file():
        return None
    try:
        return reader(path)
    except ValueError as error:
        logger.warning(
            "Ignoring malformed persisted document",
            path=path.as_posix(),
            error=str(error),
        )
        return None


LDB_CONFIG_FILE_NAME = "ldb-config.json"


HARBOR_CONFIG_FILE_NAME = "config.json"
"""The run root's Harbor job config, as Harbor writes one at a job directory."""


HARBOR_RESULT_FILE_NAME = "result.json"
"""The run root's Harbor job result, as Harbor writes one at a job directory."""


@dataclass(frozen=True)
class OutputDocument:
    """One JSON document destined for a canonical run path."""

    path: Path
    content: str


@dataclass(frozen=True)
class OutputTree:
    """One staged directory tree destined for a canonical run path."""

    source: Path
    path: Path


class RunOutputConfig(BaseModel):
    """Execution identity sidecar; the manifest remains the request authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    started_at: datetime
    trial_count: int = Field(ge=0)
    n_concurrent: int = Field(gt=0)

    def write(self, path: Path) -> None:
        """Persist this run's execution identity sidecar."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def summary(self) -> ExecutionSummary:
        """Project execution identity into the reader-facing result schema."""
        return ExecutionSummary(
            job_id=self.job_id,
            started_at=self.started_at,
            trial_count=self.trial_count,
            n_concurrent=self.n_concurrent,
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load a persisted execution identity sidecar."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def initialize_run_outputs(
    run_dir: Path,
    request: Request,
    launches: tuple[TrialLaunch, ...],
) -> None:
    """Persist the output identity before the manifest and prompt files."""
    RunOutputConfig(
        job_id=UUID(bytes=os.urandom(16), version=4),
        started_at=datetime.now(UTC),
        trial_count=len(launches),
        n_concurrent=request.n_concurrent,
    ).write(run_dir / LDB_CONFIG_FILE_NAME)


def final_run_documents(
    run: Run, report: RunReport, results: tuple[TrialResult, ...]
) -> tuple[OutputDocument, ...]:
    """Serialize every slot's LDB trial report and the run's Harbor job result.

    The job result is the one Harbor writes at a job directory, without the
    per-trial results each slot already holds.
    """
    launches = {launch.trial_name: launch for launch in run.launches()}
    config = RunOutputConfig.load(run.dir / LDB_CONFIG_FILE_NAME)
    finished = datetime.now(UTC)
    job_result = JobResult(
        id=config.job_id,
        started_at=config.started_at,
        updated_at=finished,
        finished_at=finished,
        n_total_trials=config.trial_count,
        stats=JobStats.from_trial_results(
            list(results), n_total_trials=config.trial_count
        ),
    )
    return (
        *(
            OutputDocument(
                run.slot(launches[trial.trial_name]).dir / TRIAL_REPORT_FILE_NAME,
                trial.model_dump_json(indent=2) + "\n",
            )
            for trial in report.attempts
        ),
        OutputDocument(
            run.dir / HARBOR_RESULT_FILE_NAME,
            job_result.model_dump_json(indent=2, exclude={"trial_results"}) + "\n",
        ),
    )


def replace_run_output_documents(
    documents: tuple[OutputDocument, ...],
    *,
    completion_marker: Path,
    trees: tuple[OutputTree, ...] = (),
) -> None:
    """Replace completed run documents and trees transactionally, marker last."""
    paths = (*(document.path for document in documents), *(tree.path for tree in trees))
    if len(set(paths)) != len(paths) or completion_marker not in paths:
        raise ValueError(
            "Update outputs require unique paths and one completion marker."
        )
    for tree in trees:
        if not tree.source.is_dir():
            raise ValueError(
                f"Run update source directory does not exist: {tree.source}"
            )
    ordered = tuple(
        document for document in documents if document.path != completion_marker
    ) + tuple(document for document in documents if document.path == completion_marker)
    write_run_output_documents(ordered, trees)


def write_run_output_documents(
    documents: tuple[OutputDocument, ...], trees: tuple[OutputTree, ...] = ()
) -> None:
    """Atomically replace output documents and trees, restoring them on failure."""
    staged_documents = _stage_documents(documents)
    try:
        staged_trees = _stage_trees(trees)
    except OSError:
        _discard_paths(path for _document, path in staged_documents)
        raise
    staged = (
        *((document.path, path) for document, path in staged_documents),
        *((tree.path, path) for tree, path in staged_trees),
    )
    backups: list[tuple[Path, Path]] = []
    try:
        for path, _staged in staged:
            if path.exists():
                backup = path.with_name(f".{path.name}.{secrets.token_hex(8)}.previous")
                path.replace(backup)
                backups.append((path, backup))
        for path, staged_path in staged:
            staged_path.replace(path)
    except OSError:
        _discard_paths(staged_path for _path, staged_path in staged)
        _discard_paths(path for path, _staged_path in staged)
        for path, backup in backups:
            backup.replace(path)
        raise
    _discard_paths(backup for _path, backup in backups)


def _stage_documents(
    documents: tuple[OutputDocument, ...],
) -> tuple[tuple[OutputDocument, Path], ...]:
    staged = tuple(
        (
            document,
            document.path.with_name(
                f".{document.path.name}.{secrets.token_hex(8)}.tmp"
            ),
        )
        for document in documents
    )
    try:
        for document, staged_path in staged:
            document.path.parent.mkdir(parents=True, exist_ok=True)
            staged_path.write_text(document.content, encoding="utf-8")
    except OSError:
        _discard_paths(path for _document, path in staged)
        raise
    return staged


def _stage_trees(
    trees: tuple[OutputTree, ...],
) -> tuple[tuple[OutputTree, Path], ...]:
    staged = tuple(
        (
            tree,
            tree.path.with_name(f".{tree.path.name}.{secrets.token_hex(8)}.tmp"),
        )
        for tree in trees
    )
    try:
        for tree, staged_path in staged:
            tree.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(tree.source, staged_path)
    except OSError:
        _discard_paths(path for _tree, path in staged)
        raise
    return staged


def _discard_paths(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def expand(request: Request, *, run_timestamp: datetime) -> tuple[TrialLaunch, ...]:
    """Expand one validated request into distinct deterministic trial launches.

    `run_timestamp` is part of every Evaluation Phase trial's identity, so the same
    cell planned by two runs never shares a trial name.
    """
    match request:
        case Job():
            launches = _expand_job(request, run_timestamp)
        case AuthorJob():
            launches = _expand_author(request)
        case ReplayJob():
            launches = _expand_replay(request)
    _validate_launches(launches)
    return launches


def _expand_job(job: Job, run_timestamp: datetime) -> tuple[TrialLaunch, ...]:
    return tuple(
        EvaluationLaunch(
            problem=problem,
            arm=arm,
            prompt_template=job.prompt_template,
            attempt=attempt,
            run_timestamp=run_timestamp,
            environment=job.environment,
            verifier_env=job.verifier_env,
        )
        for arm in job.arms
        for problem in job.problems
        for attempt in job.attempts
        if condition_applies(arm.condition, problem)
    )


def _expand_author(job: AuthorJob) -> tuple[TrialLaunch, ...]:
    return tuple(
        DesignLaunch(
            task=task,
            agent=job.agent,
            prompt_template=job.prompt_template,
            attempt=attempt,
            environment=job.environment,
        )
        for task in job.tasks
        for attempt in job.attempts
    )


def _expand_replay(job: ReplayJob) -> tuple[TrialLaunch, ...]:
    source = Run.open(job.source)
    return tuple(
        _replay_launch(launch, source.slot(launch), job)
        for launch in source.launches()
        if _matches_replay_filters(launch, job)
    )


def _replay_launch(launch: TrialLaunch, slot: Slot, job: ReplayJob) -> TrialLaunch:
    replay = AgentConfig(
        import_path=REPLAY_AGENT_IMPORT_PATH,
        model_name="artifact-replay",
        kwargs={
            "artifacts_dir": str(slot.dir / "artifacts"),
            "replay_install_cmd": _replay_install_cmd(launch),
        },
    )
    if isinstance(launch, DesignLaunch):
        return launch.model_copy(
            update={
                "agent": replay,
                "prompt_template": None,
                "task_override": job.task_override,
                "environment": job.environment,
            }
        )
    return launch.model_copy(
        update={
            "arm": launch.arm.model_copy(update={"agent": replay}),
            "prompt_template": None,
            "trial_name_override": launch.trial_name,
            "task_override": job.task_override,
            "environment": job.environment,
            "verifier_env": _skip_tests_env(slot) if job.skip_tests else {},
        }
    )


def _skip_tests_env(slot: Slot) -> dict[str, str]:
    """Have `test.sh` skip the behavioral tests and score the recorded counts."""
    counts = slot.behavioral_counts()
    if counts is None:
        raise ValueError(f"Slot has no recorded behavioral counts: {slot.dir}")
    passed, total = counts
    return {"LDB_SKIP_TESTS": "1", "LDB_PASSED": str(passed), "LDB_TOTAL": str(total)}


def _matches_replay_filters(launch: TrialLaunch, job: ReplayJob) -> bool:
    if job.tasks and launch.task.name not in job.tasks:
        return False
    if job.trials and launch.trial_name not in job.trials:
        return False
    return not (
        job.problems
        and (
            not isinstance(launch, EvaluationLaunch)
            or launch.problem.name not in job.problems
        )
    )


def _replay_install_cmd(launch: TrialLaunch) -> str | None:
    if isinstance(launch, EvaluationLaunch) and isinstance(
        launch.arm.condition, NoLibrary
    ):
        return None
    return launch.task.replay_install_cmd


def _validate_launches(launches: tuple[TrialLaunch, ...]) -> None:
    if not launches:
        raise ValueError("Request expands to no trials")
    names = tuple(launch.trial_name for launch in launches)
    if len(set(names)) != len(names):
        raise ValueError("Request expands to duplicate trial names")


@dataclass(frozen=True)
class AuthoredLibrary:
    """One authored library workspace and any evidence it is unavailable."""

    workspace: Path
    incomplete_reason: str | None


def source_run_dir(source: Path) -> Path | None:
    """Find the design run whose Harbor manifest records this workspace.

    Layout only: the trial's artifact manifest names the workspace and the
    trial sits under `design_results/`. Whether that run authored anything is
    the caller's question, answered from its own persisted report rather than
    by validating its manifest here.
    """
    trial_dir = next(
        (
            parent
            for parent in source.parents
            if TrialPaths(parent).artifacts_manifest_path.is_file()
        ),
        None,
    )
    if trial_dir is None or trial_dir.parent.name != DESIGN_RESULTS_DIR_NAME:
        return None
    try:
        recorded = workspace_artifact(trial_dir)
    except (RegradeError, ValueError):
        return None
    return trial_dir.parent.parent if recorded == source else None


def read_authored_artifact(run: Run, launch: DesignLaunch) -> AuthoredLibrary:
    """Read the one authored workspace without treating absence as a run error."""
    slot = run.slot(launch)
    if slot.result() is None:
        reason = "missing Harbor result"
    elif (error := slot.workspace_error()) is not None:
        reason = f"missing Design Phase workspace artifact: {error}"
    else:
        reason = None
    workspace = (
        workspace_artifact(slot.dir)
        if reason is None
        else TrialPaths(slot.dir).host_artifact_path(
            MAIN_SERVICE_NAME, WORKSPACE_LOCATION
        )
    )
    logger.debug(
        "Resolved Design Phase library availability.",
        source_run=run.dir.as_posix(),
        task=launch.task.name,
        author_attempt=launch.attempt,
        workspace=workspace.as_posix(),
        available=reason is None,
        incomplete_reason=reason,
    )
    return AuthoredLibrary(workspace=workspace, incomplete_reason=reason)


SANDBOX_USAGE_FILE_NAME = "sandbox-usage.json"


def load_sandbox_usage(
    trial_dir: Path,
    environment_type: str,
    ledger_path: Path,
) -> SandboxUsageReport:
    """Load snapshotted spend, treating local Docker as no external charge."""
    if environment_type == "docker":
        return SandboxUsageReport(cost_usd=0.0)
    sidecar = trial_dir / SANDBOX_USAGE_FILE_NAME
    path = ledger_path if ledger_path.is_file() else sidecar
    if not path.is_file():
        return SandboxUsageReport()
    return SandboxUsageReport.model_validate_json(path.read_text(encoding="utf-8"))


REPLAY_AGENT_IMPORT_PATH = "lib_design_bench.harbor.agents:ReplayArtifactsAgent"
