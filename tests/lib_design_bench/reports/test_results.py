"""Every run publishes one result from typed trial evidence."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from harbor.models.trial.config import AgentConfig
from pydantic import ValidationError

from lib_design_bench.metrics.costs import CostRates
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.conditions import NoLibrary
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import ReplayJob
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.models.reports import UsageReport
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.reports.rebuild import rebuild_reports
from lib_design_bench.reports.results import agent_details
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import Run
from tests.lib_design_bench.conftest import MINIMAL_CONFIG
from tests.lib_design_bench.conftest import RecordingQueue
from tests.lib_design_bench.conftest import experiment_dir
from tests.lib_design_bench.conftest import invoke_experiment
from tests.lib_design_bench.conftest import seed_finished_slot
from tests.lib_design_bench.conftest import seed_slot_artifacts


def test_design_and_evaluation_have_one_versioned_result_each(
    tmp_path: Path, tasks_root: Path
) -> None:
    """Authoring identity belongs to libraries and eval identity to implementors."""
    task = Task.from_dir(tasks_root / "pyt")
    author = AgentConfig(
        name="codex",
        model_name="model-1",
        kwargs={"version": "1.2", "reasoning_effort": "high", "auto_snapshot": True},
    )
    design_dir = tmp_path / "design"
    plan(AuthorJob(tasks=(task,), agent=author, n_concurrent=1), design_dir)
    finalize(design_dir)
    design = LdbResult.load(design_dir / "ldb-result.json")
    assert design.schema_version == 1
    assert design.meta.type == "design"
    assert design.implementors == {}
    assert design.trials == ()
    (library,) = design.libraries.values()
    assert library.author is not None
    assert library.author.version == "1.2"
    assert library.author.reasoning == "high"
    assert library.author.kwargs == {"auto_snapshot": True}

    evaluation_dir = tmp_path / "evaluation"
    plan(
        Job(
            problems=(task.problem("01_step"),),
            arms=(Arm(label="impl", condition=NoLibrary(), agent=author),),
            n_concurrent=1,
        ),
        evaluation_dir,
    )
    finalize(evaluation_dir)
    evaluation = LdbResult.load(evaluation_dir / "ldb-result.json")
    assert evaluation.meta.type == "evaluation"
    assert evaluation.implementors["impl"].version == "1.2"
    assert evaluation.trials[0].library in evaluation.libraries
    assert evaluation.trials[0].implementor == "impl"
    assert "attempts" not in (evaluation_dir / "ldb-result.json").read_text()


def test_replay_keeps_original_agent_without_claiming_agent_spend(
    tmp_path: Path, tasks_root: Path
) -> None:
    """A verifier replay identifies the saved author, not its replay agent."""
    task = Task.from_dir(tasks_root / "pyt")
    source = tmp_path / "source"
    plan(
        AuthorJob(
            tasks=(task,),
            agent=AgentConfig(name="codex", model_name="m"),
            n_concurrent=1,
        ),
        source,
    )
    finalize(source)
    replay = tmp_path / "replay"
    plan(ReplayJob(source=source.resolve(), n_concurrent=1), replay)
    finalize(replay)
    result = LdbResult.load(replay / "ldb-result.json")
    assert result.meta.type == "replay"
    assert result.meta.source_run == "../source"
    assert result.trials == ()
    (library,) = result.libraries.values()
    assert library.author is not None and library.author.agent == "codex"
    assert library.cost is None


def test_evaluation_replay_keeps_the_original_implementor(
    tmp_path: Path, tasks_root: Path
) -> None:
    """Replaying an evaluation has no fresh model usage or replay-agent identity."""
    task = Task.from_dir(tasks_root / "pyt")
    source = tmp_path / "source"
    plan(
        Job(
            problems=(task.problem("01_step"),),
            arms=(
                Arm(
                    label="impl",
                    condition=NoLibrary(),
                    agent=AgentConfig(
                        name="codex",
                        model_name="m",
                        kwargs={
                            "version": "3",
                            "max_tokens": 2048,
                            "settings": [{"api_key": "never-save", "max_tokens": 512}],
                        },
                    ),
                ),
            ),
            n_concurrent=1,
        ),
        source,
    )
    finalize(source)
    replay_dir = tmp_path / "replay"
    plan(ReplayJob(source=source.resolve(), n_concurrent=1), replay_dir)
    finalize(replay_dir)
    result = LdbResult.load(replay_dir / "ldb-result.json")
    assert result.meta.type == "replay"
    assert result.implementors["impl"].agent == "codex"
    assert result.implementors["impl"].version == "3"
    assert result.implementors["impl"].kwargs == {
        "max_tokens": 2048,
        "settings": [{"max_tokens": 512}],
    }
    assert "never-save" not in (replay_dir / "ldb-result.json").read_text()
    assert result.trials[0].cost is None
    assert result.trials[0].input_tokens is None


def test_root_scoreboard_can_be_rebuilt_from_trial_owned_pricing(
    tmp_path: Path, tasks_root: Path
) -> None:
    """Deleting only the scoreboard never erases a trial's repricing history."""
    task = Task.from_dir(tasks_root / "pyt")
    run_dir = tmp_path / "design"
    (launch,) = plan(
        AuthorJob(tasks=(task,), agent=AgentConfig(name="oracle"), n_concurrent=1),
        run_dir,
    )
    finalize(run_dir)
    slot = Run.open(run_dir).slot(launch)
    trial = slot.trial_report()
    assert trial is not None
    slot_report = trial.model_copy(
        update={
            "usage": UsageReport(
                input_tokens=2_000_000,
                uncached_input_tokens=1_500_000,
                cache_input_tokens=500_000,
                output_tokens=250_000,
                cost_usd=0.75,
            )
        }
    )
    (slot.dir / "trial-report.json").write_text(slot_report.model_dump_json())
    rates = CostRates(
        input_per_million=2, output_per_million=8, cache_input_per_million=1
    )
    current = Run.open(run_dir)
    rebuild_reports(
        current, {launch.trial_name: None}, (), repricing={launch.trial_name: rates}
    )
    priced = current.slot(launch).trial_report()
    assert priced is not None and priced.usage.cost_usd == 5.5
    (run_dir / "ldb-result.json").unlink()
    finalize(run_dir)
    saved = LdbResult.load(run_dir / "ldb-result.json")
    assert saved.libraries[f"{task.name}/a1"].cost == 5.5
    retained = current.slot(launch).trial_report()
    assert retained is not None
    assert retained.usage.reported_cost_usd == 0.75
    assert retained.usage.input_cost_per_million == 2


def test_authored_libraries_from_distinct_runs_keep_distinct_ids_and_authors(
    tmp_path: Path, tasks_root: Path
) -> None:
    """Identical task/attempt/workspace names do not collapse across sources."""
    task = Task.from_dir(tasks_root / "pyt")
    arms = []
    for name in ("alice", "bob"):
        source = tmp_path / name
        (launch,) = plan(
            AuthorJob(tasks=(task,), agent=AgentConfig(name=name), n_concurrent=1),
            source,
        )
        slot = Run.open(source).slot(launch)
        seed_slot_artifacts(slot)
        arms.append(
            Arm(
                label=name,
                condition=AuthoredArtifact(
                    task=task,
                    source=(slot.dir / "artifacts" / "workspace").resolve(),
                    attempt=1,
                    problems=("01_step",),
                ),
                agent=AgentConfig(name="oracle"),
            )
        )
    evaluation = tmp_path / "evaluation"
    plan(
        Job(problems=(task.problem("01_step"),), arms=tuple(arms), n_concurrent=1),
        evaluation,
    )
    finalize(evaluation)
    result = LdbResult.load(evaluation / "ldb-result.json")
    assert len(result.libraries) == 2
    assert {
        library.author.agent
        for library in result.libraries.values()
        if library.author is not None
    } == {"alice", "bob"}
    assert len({trial.library for trial in result.trials}) == 2
    assert {library.source_run for library in result.libraries.values()} == {
        "../alice",
        "../bob",
    }


def test_implementor_id_cannot_name_two_agent_configurations(tasks_root: Path) -> None:
    """One registry entry cannot silently overwrite another implementor."""
    task = Task.from_dir(tasks_root / "pyt")
    with pytest.raises(ValidationError, match="conflicting agent configs"):
        Job(
            problems=(task.problem("01_step"),),
            arms=(
                Arm(
                    label="impl",
                    condition=NoLibrary(),
                    agent=AgentConfig(name="oracle"),
                ),
                Arm(
                    label="impl",
                    condition=NoLibrary(problems=("02_step",)),
                    agent=AgentConfig(name="codex"),
                ),
            ),
            n_concurrent=1,
        )


def _fail_scoreboard_replace(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Fail the real atomic writer only as it installs the result document."""
    replace = Path.replace

    def fail(self: Path, destination: Path) -> Path:
        if destination == target:
            raise OSError("injected output failure")
        return replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail)


def test_interrupted_scoreboard_write_rebuilds_from_trial_evidence(
    tmp_path: Path, tasks_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IO failure after the trial transaction leaves evidence recoverable."""
    task = Task.from_dir(tasks_root / "pyt")
    run_dir = tmp_path / "evaluation"
    (launch,) = plan(
        Job(
            problems=(task.problem("01_step"),),
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
    finalize(run_dir)
    slot = Run.open(run_dir).slot(launch)
    prior = slot.trial_report()
    assert prior is not None
    (slot.dir / "trial-report.json").write_text(
        prior.model_copy(
            update={"usage": UsageReport(cost_usd=1.25, reported_cost_usd=1.25)}
        ).model_dump_json()
    )
    with monkeypatch.context() as patch:
        _fail_scoreboard_replace(patch, run_dir / "ldb-result.json")
        with pytest.raises(OSError, match="injected output failure"):
            finalize(run_dir)
    retained = Run.open(run_dir).slot(launch).trial_report()
    assert retained is not None and retained.usage.cost_usd == 1.25
    finalize(run_dir)
    result = LdbResult.load(run_dir / "ldb-result.json")
    assert result.trials[0].name == launch.trial_name
    assert result.trials[0].cost == 1.25


def test_interrupted_child_publication_recovers_experiment_root(
    tmp_path: Path,
    tasks_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_queue: RecordingQueue,
) -> None:
    """Re-finalizing the child republishes the sole experiment scoreboard."""
    created = invoke_experiment(MINIMAL_CONFIG, tasks_root, tmp_path / "runs")
    assert created.exit_code == 0, created.output
    root = experiment_dir(tmp_path / "runs")
    child = root / "evaluation_results"
    before = {
        p.relative_to(child).as_posix(): p.read_bytes()
        for p in child.glob("*/result.json")
    }
    with monkeypatch.context() as patch:
        _fail_scoreboard_replace(patch, root / "ldb-result.json")
        with pytest.raises(OSError, match="injected output failure"):
            finalize(child)
    assert {
        p.relative_to(child).as_posix(): p.read_bytes()
        for p in child.glob("*/result.json")
    } == before
    finalize(child)
    result = LdbResult.load(root / "ldb-result.json")
    assert result.meta.type == "experiment"
    assert result.meta.complete is True
    assert len(result.trials) == len(Run.open(child).launches())
    assert [
        path.relative_to(root).as_posix() for path in root.rglob("ldb-result.json")
    ] == ["ldb-result.json"]

    first, second, *_ = Run.open(child).launches()
    first_slot = Run.open(child).slot(first)
    first_result = first_slot.result()
    assert first_result is not None
    (first_slot.dir / "result.json").write_text(
        first_result.model_copy(
            update={"finished_at": datetime.now(UTC)}
        ).model_dump_json()
    )
    finalize(child)
    partly_dated = LdbResult.load(root / "ldb-result.json")
    assert partly_dated.meta.complete is True
    assert partly_dated.meta.finished_at is None
    (Run.open(child).slot(second).dir / "result.json").unlink()
    finalize(child)
    incomplete = LdbResult.load(root / "ldb-result.json")
    assert incomplete.meta.complete is False
    assert incomplete.meta.finished_at is None


def test_agent_metadata_filters_common_credential_key_forms() -> None:
    """Nested metadata retains ordinary settings without exposing credentials."""
    agent = AgentConfig(
        name="codex",
        model_name="m",
        kwargs={
            "secret": "private",
            "apiKey": "private",
            "clientSecret": "private",
            "accessToken": "private",
            "AWS_ACCESS_KEY_ID": "private",
            "headers": {"X-Custom-Authorization": "private"},
            "env": {"UNKNOWN_CREDENTIAL": "private"},
            "settings": [{"password": "private", "max_tokens": 128}],
            "max_tokens": 4096,
        },
    )
    assert agent_details(agent).kwargs == {
        "settings": [{"max_tokens": 128}],
        "max_tokens": 4096,
    }


def test_malformed_trial_evidence_is_not_replaced_during_rebuild(
    tmp_path: Path, tasks_root: Path
) -> None:
    """A truncated trial document requires repair, not silent repricing loss."""
    task = Task.from_dir(tasks_root / "pyt")
    run_dir = tmp_path / "run"
    (launch,) = plan(
        AuthorJob(tasks=(task,), agent=AgentConfig(name="oracle"), n_concurrent=1),
        run_dir,
    )
    finalize(run_dir)
    path = Run.open(run_dir).slot(launch).dir / "trial-report.json"
    path.write_text('{"schema_version":1,"usage":')
    with pytest.raises(ValidationError):
        finalize(run_dir)
    assert path.read_text() == '{"schema_version":1,"usage":'


def test_trial_that_spent_its_limit_leaves_the_result_complete(
    tmp_path: Path, tasks_root: Path, fixtures_root: Path
) -> None:
    """A spent limit is a finished outcome, so it never marks a result incomplete."""
    task = Task.from_dir(tasks_root / "pyt")
    run_dir = tmp_path / "evaluation"
    (launch,) = plan(
        Job(
            problems=(task.problem("01_step"),),
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
    seed_finished_slot(slot, fixtures_root)
    path = slot.dir / "result.json"
    document = json.loads(path.read_text())
    document["exception_info"] = {
        "exception_type": "ApiUsageLimitError",
        "exception_message": "spending cap reached",
        "exception_traceback": "ApiUsageLimitError: spending cap reached\n",
        "occurred_at": "2026-09-01T12:30:00Z",
    }
    path.write_text(json.dumps(document))

    finalize(run_dir)

    result = LdbResult.load(run_dir / "ldb-result.json")
    (trial,) = result.trials
    assert trial.outcome == "finished"
    assert trial.incomplete_reason is not None
    assert result.meta.complete is True


def test_trial_owed_more_work_keeps_its_recorded_score(
    tmp_path: Path, tasks_root: Path, fixtures_root: Path
) -> None:
    """A graded slot that still owes measurement is reported with its score."""
    task = Task.from_dir(tasks_root / "pyt")
    run_dir = tmp_path / "evaluation"
    (launch,) = plan(
        Job(
            problems=(task.problem("01_step"),),
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
    seed_finished_slot(slot, fixtures_root)
    seed_slot_artifacts(slot)
    path = slot.dir / "result.json"
    document = json.loads(path.read_text())
    document["verifier_result"]["rewards"] = {"reward": 0.5, "pass_rate": 0.75}
    path.write_text(json.dumps(document))

    finalize(run_dir)

    result = LdbResult.load(run_dir / "ldb-result.json")
    (trial,) = result.trials
    assert trial.outcome == "reanalyze"
    assert trial.score == 0.5
