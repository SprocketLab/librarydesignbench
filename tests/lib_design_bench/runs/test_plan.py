"""Expansion contracts for typed trial requests."""

from __future__ import annotations

import importlib
import shutil
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import TaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import AgentInfo
from harbor.models.trial.result import ExceptionInfo
from harbor.models.trial.result import TrialResult
from pydantic import ValidationError

from lib_design_bench.harbor.agents import ReplayArtifactsAgent
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import ReplayJob
from lib_design_bench.models.manifest import EvaluationLaunch
from lib_design_bench.models.manifest import RunManifest
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.pipeline.run import run
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import REPLAY_AGENT_IMPORT_PATH
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import expand
from tests.lib_design_bench.conftest import RecordingQueue

RUN_TIMESTAMP = datetime(2026, 9, 4, 17, 12, 22, 105392, tzinfo=UTC)


@pytest.mark.parametrize("document", ("result", "trial-report"))
def test_run_readers_distinguish_malformed_trial_evidence(
    document: str, tasks_root: Path, tmp_path: Path
) -> None:
    """Malformed Harbor input may be retried; a corrupt trial report must fail."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(label="impl", condition=NoLibrary(), agent=AgentConfig(name="oracle")),
        ),
        n_concurrent=1,
    )
    run_dir = tmp_path / "run"
    (launch,) = plan(request, run_dir)
    persisted = Run.open(run_dir)
    slot = persisted.slot(launch)
    paths_and_readers = {
        "result": (slot.dir / "result.json", slot.result),
        "trial-report": (slot.dir / "trial-report.json", slot.trial_report),
    }
    path, reader = paths_and_readers[document]

    assert reader() is None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    if document == "trial-report":
        with pytest.raises(ValidationError):
            reader()
    else:
        assert reader() is None


def test_run_needs_no_host_formatter(
    tasks_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_queue: RecordingQueue,
) -> None:
    """Host PATH does not control verifier formatting inside Harbor."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(label="impl", condition=NoLibrary(), agent=AgentConfig(name="oracle")),
        ),
        n_concurrent=1,
    )
    run_dir = tmp_path / "run"
    launches = plan(request, run_dir)
    stale_slot = run_dir / launches[0].trial_name
    stale_slot.mkdir()
    (stale_slot / "stale.txt").write_text("previous execution")
    previous_result = result_for(
        launches[0].trial_name, exception_type="AgentError"
    ).model_dump_json()
    (stale_slot / "result.json").write_text(previous_result)

    monkeypatch.setenv("PATH", "")

    run(launches, run_dir, n_concurrent=1, debug_build_contexts=False)
    assert len(recording_queue.configs) == 1

    assert not (run_dir / "build-contexts").exists()
    assert (stale_slot / "result.json").read_text() != previous_result
    assert not (stale_slot / "stale.txt").exists()
    assert not tuple(run_dir.glob(".harbor-*"))


def test_run_requires_static_references_before_mutating_slots_or_launching_harbor(
    tasks_root: Path,
    tmp_path: Path,
    recording_queue: RecordingQueue,
) -> None:
    """A missing scoring input aborts the complete batch before trial preparation."""
    task_dir = tmp_path / "task"
    shutil.copytree(tasks_root / "pyt", task_dir)
    (task_dir / "evaluation/01_step/tests/static_reference.json").unlink()
    task = Task.from_dir(task_dir)
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(label="impl", condition=NoLibrary(), agent=AgentConfig(name="oracle")),
        ),
        n_concurrent=1,
    )
    run_dir = tmp_path / "run"
    launches = plan(request, run_dir)
    stale_slot = run_dir / launches[0].trial_name
    stale_slot.mkdir()
    marker = stale_slot / "stale.txt"
    marker.write_text("previous execution", encoding="utf-8")

    with pytest.raises(ValueError, match="Missing static reference"):
        run(launches, run_dir, n_concurrent=1, debug_build_contexts=False)

    assert marker.read_text(encoding="utf-8") == "previous execution"
    assert not recording_queue.configs
    assert not (run_dir / "build-contexts").exists()


def test_missing_authored_library_skips_run(
    tasks_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable library is retained for finalization without host tooling."""
    task = Task.from_dir(tasks_root / "pyt")
    workspace = tmp_path / "source-run" / "author-slot" / "artifacts" / "workspace"
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(
                label="impl",
                condition=AuthoredArtifact(
                    task=task,
                    source=workspace.resolve(),
                    attempt=1,
                    problems=("01_step",),
                    incomplete_reason="missing Design Phase workspace artifact",
                ),
                agent=AgentConfig(name="oracle"),
            ),
        ),
        n_concurrent=1,
    )
    run_dir = tmp_path / "run"
    launches = plan(request, run_dir)
    stale_slot = run_dir / launches[0].trial_name
    stale_slot.mkdir()
    (stale_slot / "stale.txt").write_text("previous execution")
    monkeypatch.setenv("PATH", "")
    run(
        launches,
        run_dir,
        n_concurrent=1,
        debug_build_contexts=False,
    )

    assert not (run_dir / "build-contexts").exists()
    assert not stale_slot.exists()
    report = finalize(run_dir)

    assert report.run_type == "evaluation"
    assert len(report.attempts) == 1
    assert report.attempts[0].score == 0.0
    assert report.attempts[0].incomplete_reason is not None
    assert "missing Design Phase workspace" in report.attempts[0].incomplete_reason


def test_run_timestamp_prevents_cross_run_trial_name_collisions(
    tasks_root: Path, tmp_path: Path
) -> None:
    """The persisted run timestamp uniquely and repeatably names the same cell."""
    task = Task.from_dir(tasks_root / "pyt")
    request = Job(
        problems=(task.problem("01_step"),),
        arms=(
            Arm(label="impl", condition=NoLibrary(), agent=AgentConfig(name="oracle")),
        ),
        n_concurrent=1,
    )
    first_timestamp = datetime(2026, 9, 3, 23, 53, 3, 252616, tzinfo=UTC)
    first_manifest = RunManifest(
        repo_commit="commit",
        tasks_hash="hash",
        timestamp=first_timestamp,
        request=request,
    )
    second_manifest = first_manifest.model_copy(
        update={"timestamp": datetime(2026, 9, 4, 17, 12, 22, 178640, tzinfo=UTC)}
    )

    first = Run(tmp_path / "first", first_manifest).launches()[0].trial_name
    repeated = Run(tmp_path / "first", first_manifest).launches()[0].trial_name
    second = Run(tmp_path / "second", second_manifest).launches()[0].trial_name

    assert first == repeated
    assert first != second
    assert first.startswith("pyt-no-library-a1__oracle__01_step__")
    assert len(first.rsplit("__", 1)[1]) == 8

    planned_dir = tmp_path / "planned"
    planned = plan(request, planned_dir)
    assert planned == Run.open(planned_dir).launches()


def test_expand_author_and_replay_requests(tasks_root: Path, tmp_path: Path) -> None:
    """Author attempts and replays preserve logical trial identity."""
    agent = AgentConfig(name="oracle")
    author = AuthorJob(
        tasks=(Task.from_dir(tasks_root / "pyt"),),
        agent=agent,
        attempts=(1, 2),
        n_concurrent=1,
    )
    assert tuple(
        launch.trial_name for launch in expand(author, run_timestamp=RUN_TIMESTAMP)
    ) == (
        "pyt__phase-1__author__a1",
        "pyt__phase-1__author__a2",
    )
    source = tmp_path / "source"
    plan(author, source)

    replay = ReplayJob(source=source.resolve(), n_concurrent=1)
    assert tuple(
        launch.trial_name for launch in expand(replay, run_timestamp=RUN_TIMESTAMP)
    ) == (
        "pyt__phase-1__author__a1",
        "pyt__phase-1__author__a2",
    )
    assert all(
        launch.prompt_path(tmp_path) is None
        for launch in expand(replay, run_timestamp=RUN_TIMESTAMP)
    )
    replay_dir = tmp_path / "replay"
    (replay_launch,) = plan(
        ReplayJob(
            source=source.resolve(),
            trials=("pyt__phase-1__author__a1",),
            n_concurrent=1,
        ),
        replay_dir,
    )
    assert Run.open(replay_dir).slot(replay_launch).dir == (
        replay_dir / replay_launch.trial_name
    )
    assert replay_launch.agent.kwargs["artifacts_dir"] == str(
        source / "design_results" / replay_launch.trial_name / "artifacts"
    )
    override = tmp_path / "design-override"
    assert tuple(
        launch.task_dir
        for launch in expand(
            ReplayJob(
                source=source.resolve(),
                task_override=override.resolve(),
                n_concurrent=1,
            ),
            run_timestamp=RUN_TIMESTAMP,
        )
    ) == (override.resolve(), override.resolve())

    evaluation_source = tmp_path / "evaluation-source"
    evaluation = Job(
        problems=(Task.from_dir(tasks_root / "pyt").problem("01_step"),),
        arms=(Arm(label="impl", condition=NoLibrary(), agent=agent),),
        n_concurrent=1,
    )
    (source_evaluation_launch,) = plan(evaluation, evaluation_source)
    evaluation_override = tmp_path / "evaluation-override"
    evaluation_replay = expand(
        ReplayJob(
            source=evaluation_source.resolve(),
            task_override=evaluation_override.resolve(),
            n_concurrent=1,
        ),
        run_timestamp=RUN_TIMESTAMP,
    )
    assert tuple(launch.task_dir for launch in evaluation_replay) == (
        evaluation_override.resolve(),
    )
    assert tuple(launch.trial_name for launch in evaluation_replay) == (
        source_evaluation_launch.trial_name,
    )
    replay_run_dir = tmp_path / "evaluation-replay"
    replay_launches = plan(
        ReplayJob(source=evaluation_source.resolve(), n_concurrent=1), replay_run_dir
    )
    assert replay_launches == Run.open(replay_run_dir).launches()


def test_plan_persists_each_request_and_renders_only_requested_prompts(
    tasks_root: Path, tmp_path: Path
) -> None:
    """Fresh plans persist their request and write one prompt per templated arm."""
    task = Task.from_dir(tasks_root / "pyt")
    agent = AgentConfig(name="oracle")
    existing_name = next(iter(task.existing_libraries))
    job = Job(
        problems=task.active_problems(),
        arms=(
            Arm(
                label="impl",
                condition=ExistingLibrary(
                    task=task,
                    name=existing_name,
                    entry=task.existing_libraries[existing_name],
                ),
                agent=agent,
            ),
            Arm(label="impl", condition=NoLibrary(), agent=agent),
        ),
        prompt_template=(
            "{{instruction}}\n\nRuntime: {{runtime}}; dependencies: {{libraries}}"
        ),
        n_concurrent=1,
    )
    author = AuthorJob(
        tasks=(task,),
        agent=agent,
        prompt_template="{{instruction}}",
        n_concurrent=1,
    )

    job_dir = tmp_path / "job"
    author_dir = tmp_path / "author"
    assert plan(job, job_dir) == Run.open(job_dir).launches()
    prompt = (job_dir / "prompts" / f"pyt__impl__{existing_name}.md").read_text()
    assert prompt.startswith("{{instruction}}\n\nRuntime: " + task.environment_runtime)
    assert f"`{existing_name}`" in prompt.split("dependencies: ")[1].split(", ")
    assert (job_dir / "prompts" / "pyt__impl__no-library.md").is_file()

    round_trip_job = Job(
        problems=(task.problem("01_step"),),
        arms=(Arm(label="impl", condition=NoLibrary(), agent=agent),),
        n_concurrent=1,
    )
    round_trip_dir = tmp_path / "round-trip-job"
    assert plan(round_trip_job, round_trip_dir) == Run.open(round_trip_dir).launches()
    assert Run.open(round_trip_dir).request() == round_trip_job
    assert not (round_trip_dir / "prompts").exists()

    assert plan(author, author_dir) == expand(author, run_timestamp=RUN_TIMESTAMP)
    assert Run.open(author_dir).request() == author
    assert all(
        path.read_text() == "{{instruction}}"
        for path in (author_dir / "prompts").glob("*.md")
    )

    replay = ReplayJob(source=author_dir.resolve(), n_concurrent=1)
    replay_dir = tmp_path / "replay"
    assert plan(replay, replay_dir) == expand(replay, run_timestamp=RUN_TIMESTAMP)
    assert Run.open(replay_dir).request() == replay
    assert not (replay_dir / "prompts").exists()


def test_existing_run_plans_from_its_manifest_after_a_hand_edit(
    tasks_root: Path, tmp_path: Path
) -> None:
    """Editing a persisted request is supported surgery, not a rejected plan."""
    task = Task.from_dir(tasks_root / "pyt")
    arm = Arm(label="impl", condition=NoLibrary(), agent=AgentConfig(name="oracle"))
    request = Job(problems=(task.problem("01_step"),), arms=(arm,), n_concurrent=1)
    run_dir = tmp_path / "run"
    (planned,) = plan(request, run_dir)

    manifest_path = run_dir / "manifest.json"
    widened = request.model_copy(
        update={
            "problems": (task.problem("01_step"), task.problem("02_step")),
            "n_concurrent": 2,
        }
    )
    RunManifest.load(manifest_path).model_copy(update={"request": widened}).write(
        manifest_path
    )

    launches = plan(request, run_dir)

    assert planned in launches
    assert {
        launch.problem.name
        for launch in launches
        if isinstance(launch, EvaluationLaunch)
    } == {"01_step", "02_step"}


def result_for(trial_name: str, *, exception_type: str) -> TrialResult:
    """Build a real Harbor result artifact for a planned launch."""
    return TrialResult(
        task_name="task",
        trial_name=trial_name,
        trial_uri=f"file:///tmp/{trial_name}",
        task_id=LocalTaskId(path=Path("/tmp")),
        task_checksum="checksum",
        config=TrialConfig(task=TaskConfig(path=Path("/tmp")), trial_name=trial_name),
        agent_info=AgentInfo(name="agent", version="1"),
        exception_info=ExceptionInfo(
            exception_type=exception_type,
            exception_message="failed",
            exception_traceback="traceback",
            occurred_at=datetime.now(UTC),
        ),
    )


def test_replay_agent_import_path_names_the_replay_agent() -> None:
    """Planning names the replay agent by path, since runs cannot import Harbor adapters."""
    module, _, name = REPLAY_AGENT_IMPORT_PATH.partition(":")
    assert getattr(importlib.import_module(module), name) is ReplayArtifactsAgent
