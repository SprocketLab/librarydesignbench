"""Public finalization behavior for persisted typed runs."""

from __future__ import annotations

import json
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
from harbor.models.verifier.result import VerifierResult

from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import ExistingLibrary
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.reports.rebuild import persisted_report
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import Slot
from tests.lib_design_bench.conftest import seed_workspace_manifest


def test_finalize_reports_completed_and_unexecuted_cells(
    tmp_path: Path, tasks_root: Path
) -> None:
    """Finalization derives every Evaluation Phase outcome from persisted public outputs."""
    task = Task.from_dir(tasks_root / "pyt")
    problem = task.problem("01_step")
    agent = AgentConfig(name="oracle")
    request = Job(
        problems=(problem,),
        arms=(
            Arm(label="impl", condition=NoLibrary(), agent=agent),
            Arm(
                label="impl",
                condition=ExistingLibrary(
                    task=task,
                    name="more-itertools",
                    entry=task.existing_libraries["more-itertools"],
                ),
                agent=agent,
            ),
            Arm(
                label="impl",
                condition=AuthoredArtifact(
                    task=task,
                    source=problem.solution_dir("no-library").resolve(),
                    attempt=1,
                    problems=("01_step",),
                ),
                agent=agent,
            ),
        ),
        prompt_template="{{instruction}}\nShared instructions",
        n_concurrent=1,
    )
    run_dir = tmp_path / "evaluation"
    launches = plan(request, run_dir)

    persisted = Run.open(run_dir)
    for launch in launches[:2]:
        _write_completed_slot(
            persisted.slot(launch).dir, problem.solution_dir("no-library")
        )

    report = finalize(run_dir)
    by_side = {attempt.library_kind: attempt for attempt in report.attempts}

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["request"]["prompt_template"] == "{{instruction}}\nShared instructions"
    )
    assert report.execution is not None
    assert report.execution.trial_count == 3
    assert {aggregate.library for aggregate in report.library_attempt_aggregates} == {
        "a1",
        "more-itertools",
        "no-library",
    }
    assert report.implementor_aggregates[0].implementor == "impl"
    assert report.implementor_aggregates[0].count == 3
    assert {attempt.environment_type for attempt in report.attempts} == {"docker"}
    assert {attempt.prompt_path for attempt in report.attempts} == {
        "prompts/pyt__impl__a1.md",
        "prompts/pyt__impl__more-itertools.md",
        "prompts/pyt__impl__no-library.md",
    }

    assert by_side["no-library"].incomplete_reason is None
    assert by_side["existing"].incomplete_reason is None
    assert by_side["agent"].incomplete_reason == "missing Harbor result"
    assert by_side["agent"].reward == 0.0
    assert by_side["agent"].simplicity == 0.0
    assert by_side["agent"].score == 0.0
    assert by_side["agent"].simplicity_ratios == {}
    assert Run.open(run_dir).slot(launches[2]).trial_report() is not None
    assert {attempt.library_kind for attempt in report.attempts} == {
        "no-library",
        "existing",
        "agent",
    }
    for launch in launches[:2]:
        slot = Run.open(run_dir).slot(launch)
        assert not (slot.dir / "static_analysis").exists()
        assert slot.trial_report() is not None

    reopened = Run.open(run_dir)
    assert persisted_report(reopened) == report
    assert finalize(run_dir) == report


def test_finalize_keeps_verifier_reward_when_agent_makes_no_tool_calls(
    tmp_path: Path, tasks_root: Path
) -> None:
    """The verifier decides the outcome even when the agent never uses a tool."""
    task = Task.from_dir(tasks_root / "pyt")
    problem = task.problem("01_step")
    run_dir = tmp_path / "agent-error-evaluation"
    launch = plan(
        Job(
            problems=(problem,),
            arms=(
                Arm(
                    label="impl",
                    condition=NoLibrary(),
                    agent=AgentConfig(name="codex"),
                ),
            ),
            n_concurrent=1,
        ),
        run_dir,
    )[0]
    trial_dir = Run.open(run_dir).slot(launch).dir
    _write_completed_slot(trial_dir, problem.solution_dir("no-library"))
    trajectory_dir = trial_dir / "agent"
    trajectory_dir.mkdir()
    (trajectory_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "agent": {"name": "codex"},
                "steps": [
                    {
                        "source": "agent",
                        "message": "answered without using the workspace",
                        "tool_calls": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = finalize(run_dir).attempts[0]

    assert report.status == "passed"
    assert report.incomplete_reason is None
    assert report.reward == 1.0
    assert report.static_analysis is not None
    assert report.score is not None and report.score > 0


def test_finalize_reports_design_run_without_static_measurement(
    tmp_path: Path, tasks_root: Path
) -> None:
    """A completed author launch has its own author-side result evidence."""
    task = Task.from_dir(tasks_root / "pyt")
    run_dir = tmp_path / "design"
    launches = plan(
        AuthorJob(tasks=(task,), agent=AgentConfig(name="oracle"), n_concurrent=1),
        run_dir,
    )
    launch = launches[0]
    _write_completed_slot(
        Run.open(run_dir).slot(launch).dir,
        task.problem("01_step").solution_dir("no-library"),
    )

    report = finalize(run_dir)

    assert report.attempts[0].library_kind == "author"
    assert report.run_type == "design"
    assert report.attempts[0].incomplete_reason is None
    slot = Run.open(run_dir).slot(launch)
    assert not (slot.dir / "static_analysis").exists()
    assert (slot.dir / "trial-report.json").is_file()
    assert finalize(run_dir) == report


@pytest.mark.parametrize("reward", [0.0, 2 / 3, 1.0, None])
@pytest.mark.parametrize(
    "exception_type",
    ["AgentTimeoutError", "VerifierTimeoutError", "AgentError", "RuntimeError"],
)
@pytest.mark.parametrize("reward_on_disk", [False, True])
def test_finalize_preserves_verifier_evidence_despite_errors(
    tmp_path: Path,
    tasks_root: Path,
    reward: float | None,
    exception_type: str,
    reward_on_disk: bool,
) -> None:
    """Timed-out submissions retain Harbor's metrics and composite reward."""
    problem = Task.from_dir(tasks_root / "pyt").problem("01_step")
    run_dir = tmp_path / "timeout"
    (launch,) = plan(
        Job(
            problems=(problem,),
            arms=(
                Arm(
                    label="impl",
                    condition=NoLibrary(),
                    agent=AgentConfig(name="oracle"),
                ),
            ),
            n_concurrent=1,
        ),
        run_dir,
    )
    slot = Run.open(run_dir).slot(launch)
    _write_completed_slot(slot.dir, problem.solution_dir("no-library"))
    result = slot.result()
    assert result is not None
    issue = ExceptionInfo(
        exception_type=exception_type,
        exception_message="time limit",
        exception_traceback="traceback",
        occurred_at=datetime.now(UTC),
    )
    result = result.model_copy(
        update={
            "exception_info": issue,
            "verifier_result": VerifierResult(rewards=_rewards(reward))
            if reward is not None and not reward_on_disk
            else None,
        }
    )
    (slot.dir / "result.json").write_text(result.model_dump_json())
    reward_path = slot.dir / "verifier" / "reward.json"
    if reward_on_disk and reward is not None:
        reward_path.parent.mkdir()
        reward_path.write_text(json.dumps(_rewards(reward)))
        reward_path.chmod(0o444)
    original_result = (slot.dir / "result.json").read_bytes()

    (report,) = finalize(run_dir).attempts

    assert report.reward == reward
    assert report.incomplete_reason == f"{exception_type}: time limit"
    assert report.had_error is True
    assert report.status == "failed"
    assert (report.static_analysis is not None) is (reward is not None)
    assert report.simplicity == (1.0 if reward is not None else 0.0)
    assert report.score == (reward or 0.0)
    if not (reward_on_disk and reward is not None):
        assert (slot.dir / "result.json").read_bytes() == original_result
    if reward_on_disk and reward is not None:
        assert json.loads(reward_path.read_text()) == _rewards(reward)
        recovered = slot.result()
        assert recovered is not None
        assert recovered.verifier_result is not None
        assert recovered.verifier_result.rewards == _rewards(reward)
    else:
        assert not reward_path.exists()
    assert finalize(run_dir).attempts == (report,)


def _write_completed_slot(trial_dir: Path, solution: Path) -> None:
    shutil.copytree(solution, trial_dir / "artifacts" / "workspace")
    seed_workspace_manifest(Slot(trial_dir))
    result = TrialResult(
        task_name="fixture-task",
        trial_name=trial_dir.name,
        trial_uri=trial_dir.as_uri(),
        task_id=LocalTaskId(path=trial_dir),
        task_checksum="fixture-checksum",
        config=TrialConfig(task=TaskConfig(path=trial_dir), trial_name=trial_dir.name),
        agent_info=AgentInfo(name="oracle", version="fixture"),
        verifier_result=VerifierResult(rewards=_rewards(1.0)),
    )
    (trial_dir / "result.json").write_text(result.model_dump_json(indent=2))


def test_each_trial_report_names_the_implementor_that_ran_it(
    tmp_path: Path, tasks_root: Path
) -> None:
    """Crossing two implementors keeps each cell's agent identity on its own report."""
    task = Task.from_dir(tasks_root / "pyt")
    problem = task.problem("01_step")
    request = Job(
        problems=(problem,),
        arms=(
            Arm(
                label="luna",
                condition=NoLibrary(),
                agent=AgentConfig(
                    name="codex",
                    model_name="gpt-5.6-luna",
                    kwargs={"reasoning_effort": "high"},
                ),
            ),
            Arm(
                label="ds4-flash",
                condition=NoLibrary(),
                agent=AgentConfig(name="oracle", model_name="deepseek4-flash"),
            ),
        ),
        n_concurrent=1,
    )
    run_dir = tmp_path / "two-implementors"
    launches = plan(request, run_dir)
    persisted = Run.open(run_dir)
    for launch in launches:
        _write_completed_slot(
            persisted.slot(launch).dir, problem.solution_dir("no-library")
        )

    report = finalize(run_dir)
    identities = {
        (attempt.agent, attempt.model, attempt.reasoning) for attempt in report.attempts
    }

    assert identities == {
        ("codex", "gpt-5.6-luna", "high"),
        ("oracle", "deepseek4-flash", "none"),
    }
    assert {attempt.library for attempt in report.attempts} == {"no-library"}
    assert len({attempt.trial_name for attempt in report.attempts}) == 2
    assert (report.agent, report.model) == ("codex", "gpt-5.6-luna")


def _rewards(score: float) -> dict[str, int | float]:
    passed = {0.0: 0, 2 / 3: 1, 1.0: 2}[score]
    return {
        "reward": score,
        "pass_rate": passed / 2,
        "simplicity": 1.0,
        "passed": passed,
        "total": 2,
        "stmts": 2,
        "sloc": 2,
        "cog_complex": 0,
        "cyc_complex": 1,
        "halstead_volume": 8.0,
        "parse_tokens": 5,
    }


def test_aggregates_cap_each_ratio_while_the_trial_keeps_raw_simplicity(
    tmp_path: Path, tasks_root: Path
) -> None:
    """Persisted aggregates cap each score ratio before taking their mean."""
    problem = Task.from_dir(tasks_root / "pyt").problem("01_step")
    run_dir = tmp_path / "compact"
    (launch,) = plan(
        Job(
            problems=(problem,),
            arms=(
                Arm(
                    label="impl",
                    condition=NoLibrary(),
                    agent=AgentConfig(name="oracle"),
                ),
            ),
            n_concurrent=1,
        ),
        run_dir,
    )
    slot = Run.open(run_dir).slot(launch)
    _write_completed_slot(slot.dir, problem.solution_dir("no-library"))
    result = slot.result()
    assert result is not None
    (slot.dir / "result.json").write_text(
        result.model_copy(
            update={
                "verifier_result": VerifierResult(
                    rewards={
                        **_rewards(1.0),
                        "reward": 0.75,
                        "simplicity": 2.0,
                        "ratio.sloc": 6.0,
                        "ratio.cyc_complex": 0.5,
                        "ratio.cog_complex": 1.0,
                        "ratio.halstead_volume": 0.5,
                    }
                )
            }
        ).model_dump_json()
    )

    report = finalize(run_dir)

    assert report.attempts[0].simplicity == 2.0
    assert report.attempts[0].score == 0.75
    assert report.implementor_aggregates[0].simplicity.mean == 0.75
    assert report.implementor_aggregates[0].score.mean == 0.75
    assert report.library_attempt_aggregates[0].problems["01_step"].simplicity == 0.75
