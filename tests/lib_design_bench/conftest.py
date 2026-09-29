"""Harbor trial-queue substitute and saved-run helpers shared by every test."""

from __future__ import annotations

import json
import shutil
from collections.abc import Coroutine
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Literal

import pytest
from harbor.models.task.id import LocalTaskId
from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.trajectory import Trajectory
from harbor.models.trial.artifact_manifest import ArtifactManifest
from harbor.models.trial.artifact_manifest import ArtifactManifestEntry
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import TaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import AgentInfo
from harbor.models.trial.result import ExceptionInfo
from harbor.models.trial.result import TrialResult
from harbor.models.verifier.result import VerifierResult
from typer.testing import CliRunner
from typer.testing import Result

from lib_design_bench.cli import app
from lib_design_bench.harbor import runner
from lib_design_bench.harbor.sandbox_costs import SandboxCostTracker
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.reports import FORMAT_FAILURE_LOG
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.replay import MEASUREMENT_REVISION
from lib_design_bench.pipeline.replay import reference_identity
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import Slot

UNREFERENCED_METRICS = {
    "stmts": 3,
    "sloc": 4,
    "cog_complex": 0,
    "cyc_complex": 1,
    "halstead_volume": 10.0,
    "parse_tokens": 22,
    "parse_recovered": False,
    "parse_error_nodes": 0,
}
"""What the fake verifier measures for a problem that has no reference yet."""


@dataclass(frozen=True)
class Outcome:
    """One fabricated Harbor trial outcome.

    `reward` is the verifier reward of the trial, and `exception` is the
    failure Harbor persisted alongside it. `raises` ends the trial before it
    writes anything, standing in for a trial stopped before Harbor persisted
    evidence.
    """

    reward: float | None = 1.0
    exception: ExceptionInfo | None = None
    raises: BaseException | None = None
    unmeasured: bool = False
    """The verifier graded behavior but formatting failed, as `test.sh` records."""


@dataclass
class RecordingQueue:
    """Records submitted trial configs and writes each trial's seeded result.

    `outcomes` selects an outcome by trial name; any other trial uses `default`.
    """

    tasks_root: Path
    batches: list[tuple[TrialConfig, ...]] = field(default_factory=list)
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    default: Outcome = Outcome()
    sandbox_costs: SandboxCostTracker | None = None
    concurrencies: list[int] = field(default_factory=list)
    """The `n_concurrent` each queue was built with, one per launched batch."""

    @property
    def configs(self) -> tuple[TrialConfig, ...]:
        """Return every submitted trial config, in submission order."""
        return tuple(config for batch in self.batches for config in batch)

    @property
    def trial_names(self) -> tuple[str, ...]:
        """Return every submitted trial name, in submission order."""
        return tuple(config.trial_name for config in self.configs)

    def add_hook(self, event: object, callback: object) -> None:
        """Accept Harbor's progress-hook registration without recording it."""
        del event, callback

    def submit_batch(
        self, configs: list[TrialConfig]
    ) -> list[Coroutine[Any, Any, TrialResult]]:
        """Record one launch batch and return its pending trial coroutines."""
        self.batches.append(tuple(configs))
        return [self._execute(config) for config in configs]

    async def _execute(self, config: TrialConfig) -> TrialResult:
        outcome = self.outcomes.get(config.trial_name, self.default)
        if outcome.raises is not None:
            raise outcome.raises
        evidence = config.trials_dir / config.trial_name
        evidence.mkdir(parents=True, exist_ok=True)
        rewards = None if outcome.reward is None else {"reward": outcome.reward}
        if config.task.path is None:
            raise ValueError("Recording queue requires a local materialized task")
        reference_path = config.task.path / "tests" / "static_reference.json"
        if outcome.reward is not None and reference_path.is_file():
            metrics = json.loads(reference_path.read_text())["metrics"]
            # Like the task's test.sh, a skip-tests run scores the counts it is
            # given instead of running the behavioral tests.
            env = config.verifier.env
            if env.get("LDB_SKIP_TESTS") == "1":
                passed = int(env.get("LDB_PASSED", "0"))
                total = int(env.get("LDB_TOTAL", "0"))
                pass_rate = passed / total if total else 0.0
            else:
                passed, total, pass_rate = int(outcome.reward == 1), 1, outcome.reward
            rewards = {
                "reward": 0.0,
                "pass_rate": pass_rate,
                "simplicity": 0.0,
                "measurement_revision": MEASUREMENT_REVISION,
                "reference_identity": reference_identity(metrics),
                "passed": passed,
                "total": total,
            }
            if outcome.unmeasured:
                (evidence / "verifier").mkdir(parents=True, exist_ok=True)
                (evidence / "verifier" / FORMAT_FAILURE_LOG).write_text(
                    "unformattable\n"
                )
            else:
                # A seeded trial measured exactly its reference, cleanly, so
                # every ratio is 1 and the reward is the squared pass rate.
                measured = {**metrics, "parse_recovered": False, "parse_error_nodes": 0}
                rewards |= {
                    "reward": pass_rate**2,
                    "simplicity": 1.0,
                    **measured,
                    **{f"ratio.{name}": 1.0 for name in metrics},
                }
                (evidence / "verifier").mkdir(parents=True, exist_ok=True)
                (evidence / "verifier" / "static_metrics.json").write_text(
                    json.dumps(measured)
                )
            (evidence / "verifier" / "reward.json").write_text(json.dumps(rewards))
        elif outcome.reward is not None and config.verifier.env.get("LDB_SKIP_TESTS"):
            # A skip-tests trial with no reference yet still measures; test.sh
            # then cannot compose a score, so the reward keeps only the counts.
            rewards = {"reward": 0.0, "pass_rate": 0.0, "simplicity": 0.0}
            (evidence / "verifier").mkdir(parents=True, exist_ok=True)
            (evidence / "verifier" / "static_metrics.json").write_text(
                json.dumps(UNREFERENCED_METRICS)
            )
        verifier = VerifierResult(rewards=rewards)
        (evidence / "artifacts" / "workspace").mkdir(parents=True, exist_ok=True)
        seed_workspace_manifest(Slot(evidence))
        result = TrialResult(
            task_name=config.task.source or config.trial_name,
            trial_name=config.trial_name,
            trial_uri=evidence.as_uri(),
            task_id=LocalTaskId(path=self.tasks_root),
            task_checksum="checksum",
            config=config,
            agent_info=AgentInfo(name="oracle", version="1"),
            started_at=datetime.now(UTC),
            verifier_result=verifier,
            exception_info=outcome.exception,
        )
        (evidence / "result.json").write_text(result.model_dump_json())
        return result


@pytest.fixture
def recording_queue(
    tasks_root: Path, monkeypatch: pytest.MonkeyPatch
) -> RecordingQueue:
    """Replace Harbor's trial queue with a recorder over seeded outcomes."""
    recorder = RecordingQueue(tasks_root=tasks_root)

    def build_queue(**kwargs: Any) -> RecordingQueue:
        recorder.concurrencies.append(int(kwargs["n_concurrent"]))
        recorder.sandbox_costs = SandboxCostTracker(kwargs["run_dir"])
        return recorder

    monkeypatch.setattr(runner, "TrialQueue", build_queue)
    return recorder


def seed_slot_failure(slot: Slot, fixtures_root: Path, fixture: str) -> None:
    """Give a persisted slot the Harbor exception one outcome fixture records.

    Only the exception is taken, so the rest of the slot stays the evidence
    its own run produced and classification reads a real saved failure.
    """
    fixture_result = json.loads(
        (fixtures_root / "outcomes" / fixture / "result.json").read_text(
            encoding="utf-8"
        )
    )
    path = slot.dir / "result.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["exception_info"] = fixture_result["exception_info"]
    path.write_text(json.dumps(document), encoding="utf-8")


def seed_finished_slot(slot: Slot, fixtures_root: Path) -> None:
    """Give a planned slot the graded Harbor result a clean trial leaves behind.

    The result carries the slot's own trial name, so a reader that matches the
    two classifies it as finished work rather than ignoring a foreign document.
    """
    document = json.loads(
        (fixtures_root / "outcomes" / "clean" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    document["trial_name"] = slot.dir.name
    slot.dir.mkdir(parents=True, exist_ok=True)
    (slot.dir / "result.json").write_text(json.dumps(document), encoding="utf-8")


def seed_slot_artifacts(slot: Slot) -> None:
    """Write the agent workspace Harbor collects, which a verifier replay reads."""
    workspace = slot.dir / "artifacts" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "solution.py").write_text("value = 1\n", encoding="utf-8")
    seed_workspace_manifest(slot)


def seed_slot_trajectory(
    slot: Slot, *sources: Literal["system", "user", "agent"]
) -> None:
    """Write the ATIF trajectory Harbor collects, one step per named originator.

    Classification reads it to tell an agent that reached the model from one
    that only ever saw its system prompt and its task.
    """
    trajectory = Trajectory(
        agent=Agent(name="codex", version="fixture"),
        steps=[
            Step(step_id=index, source=source, message="step")
            for index, source in enumerate(sources, start=1)
        ],
    )
    agent_dir = slot.dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(
        trajectory.model_dump_json(exclude_none=True), encoding="utf-8"
    )


def seed_workspace_manifest(slot: Slot) -> None:
    """Record Harbor's successful collection of the fixture workspace."""
    manifest = ArtifactManifest(
        entries=[
            ArtifactManifestEntry(
                source="/workspace",
                destination="artifacts/workspace",
                type="directory",
                status="ok",
            )
        ]
    )
    (slot.dir / "artifacts" / "manifest.json").write_text(
        json.dumps(manifest.to_json_data()), encoding="utf-8"
    )


EXPERIMENT_CONFIGS = Path(__file__).parents[1] / "fixtures" / "experiments"
"""Checked-in experiment configuration fixtures and their prompt templates."""


MINIMAL_CONFIG = EXPERIMENT_CONFIGS / "minimal.yaml"


TWO_IMPLEMENTORS_CONFIG = EXPERIMENT_CONFIGS / "two-implementors.yaml"


def invoke_experiment(
    config: Path, tasks_root: Path, output: Path, *arguments: str
) -> Result:
    """Start one experiment from a config file with the oracle design agent."""
    return CliRunner().invoke(
        app,
        [
            "run",
            str(config),
            "--agent",
            "oracle",
            "--model",
            "dummy",
            f"tasks_root={tasks_root}",
            "--output",
            str(output),
            *arguments,
        ],
    )


def continue_experiment(experiment: Path, *arguments: str) -> Result:
    """Continue a persisted experiment from its own directory."""
    return CliRunner().invoke(app, ["run", str(experiment), *arguments])


def experiment_dir(output: Path) -> Path:
    """Return the one experiment directory below an output container."""
    (experiment,) = tuple(output.iterdir())
    return experiment


def evaluation_cells(experiment: Path) -> tuple[EvaluationLaunch, ...]:
    """Return every planned cell of one experiment's evaluation run."""
    evaluation = Run.open(experiment / "evaluation_results")
    return tuple(
        launch
        for launch in evaluation.launches()
        if isinstance(launch, EvaluationLaunch)
    )


def flat_output(output: str) -> str:
    """Join one boxed CLI message into a single line, so it reads as written."""
    return " ".join(output.replace("\u2502", " ").split())


STALE_REWARDS = {
    "reward": 0.4,
    "pass_rate": 1.0,
    "simplicity": 0.25,
    "passed": 2,
    "total": 2,
}
"""A saved reward whose simplicity no longer describes the retained workspace."""


@pytest.fixture
def experiment(tmp_path: Path, tasks_root: Path) -> Path:
    """One finalized design run holding one finalized evaluation child."""
    task = Task.from_dir(tasks_root / "pyt")
    design_dir = tmp_path / "experiment"
    plan(
        AuthorJob(tasks=(task,), agent=AgentConfig(name="oracle"), n_concurrent=1),
        design_dir,
    )
    finalize(design_dir)
    evaluation_dir = design_dir / "evaluation_results"
    problem = task.problem("01_step")
    plan(
        Job(
            problems=(problem,),
            arms=(
                Arm(
                    label="impl",
                    condition=AuthoredArtifact(
                        task=task,
                        source=problem.solution_dir("no-library").resolve(),
                        attempt=1,
                        problems=("01_step",),
                    ),
                    agent=AgentConfig(name="oracle"),
                ),
            ),
            n_concurrent=1,
        ),
        evaluation_dir,
    )
    finalize(evaluation_dir)
    return design_dir


def complete_cell(experiment: Path, solution: Path) -> Slot:
    """Give the evaluation cell a saved workspace and a stale saved reward."""
    run = Run.open(experiment / "evaluation_results")
    launch = run.launches()[0]
    slot = run.slot(launch)
    shutil.copytree(solution, slot.dir / "artifacts" / "workspace")
    seed_workspace_manifest(slot)
    result = TrialResult(
        task_name="fixture-task",
        trial_name=launch.trial_name,
        trial_uri=slot.dir.as_uri(),
        task_id=LocalTaskId(path=slot.dir),
        task_checksum="fixture-checksum",
        config=TrialConfig(
            task=TaskConfig(path=slot.dir), trial_name=launch.trial_name
        ),
        agent_info=AgentInfo(name="oracle", version="fixture"),
        verifier_result=VerifierResult(rewards=STALE_REWARDS),
    )
    (slot.dir / "result.json").write_text(result.model_dump_json(indent=2))
    (slot.dir / "verifier").mkdir()
    (slot.dir / "verifier" / "reward.json").write_text(json.dumps(STALE_REWARDS))
    (slot.dir / "agent").mkdir()
    (slot.dir / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [{"source": "agent"}],
                "final_metrics": {"total_cost_usd": 0.75},
            }
        )
    )
    finalize(experiment / "evaluation_results")
    return slot
