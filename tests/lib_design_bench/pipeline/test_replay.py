"""Remeasuring saved workspaces in their task verifiers and rebuilding every report."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from harbor.models.agent.context import AgentContext
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.config import TaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import AgentInfo
from harbor.models.trial.result import TrialResult
from harbor.models.verifier.result import VerifierResult
from typer.testing import CliRunner

from lib_design_bench.cli import app
from lib_design_bench.models.conditions import AuthoredArtifact
from lib_design_bench.models.job import Arm
from lib_design_bench.models.job import AuthorJob
from lib_design_bench.models.job import Job
from lib_design_bench.models.job import default_environment
from lib_design_bench.models.manifest import TrialLaunch
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.models.reports import RunReport
from lib_design_bench.models.reports import UsageReport
from lib_design_bench.models.task import Task
from lib_design_bench.pipeline.replay import recalculate
from lib_design_bench.pipeline.run import finalize
from lib_design_bench.reports.rebuild import persisted_report
from lib_design_bench.runs.plan import plan
from lib_design_bench.runs.store import Run
from lib_design_bench.runs.store import Slot
from tests.lib_design_bench.conftest import MINIMAL_CONFIG
from tests.lib_design_bench.conftest import STALE_REWARDS
from tests.lib_design_bench.conftest import RecordingQueue
from tests.lib_design_bench.conftest import complete_cell
from tests.lib_design_bench.conftest import experiment_dir
from tests.lib_design_bench.conftest import invoke_experiment
from tests.lib_design_bench.conftest import seed_workspace_manifest


def test_recalculate_replays_the_retained_workspace_with_its_recorded_counts(
    experiment: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """The replayed measurement, not the saved number, decides a recalculated reward.

    The slot is replayed through its task's verifier with the behavioral tests
    skipped and the counts it recorded, and the 0.4 it saved survives nowhere:
    not in the Harbor result, the trial report, or the run report.
    """
    problem = Task.from_dir(tasks_root / "pyt").problem("01_step")
    slot = complete_cell(experiment, problem.solution_dir("more-itertools"))
    recording_queue.batches.clear()

    rebuilt = recalculate(
        experiment, None, n_concurrent=1, environment=default_environment()
    )

    (replayed,) = recording_queue.configs
    assert replayed.verifier.env == {
        "LDB_SKIP_TESTS": "1",
        "LDB_PASSED": "2",
        "LDB_TOTAL": "2",
    }
    result = slot.result()
    assert result is not None and result.verifier_result is not None
    assert result.verifier_result.rewards is not None
    assert result.verifier_result.rewards["reward"] == 1.0
    trial = slot.trial_report()
    assert trial is not None
    assert trial.score == 1.0
    assert trial.simplicity == 1.0
    assert trial.incomplete_reason is None
    assert rebuilt[1].report.attempts[0].score == 1.0
    report = persisted_report(Run.open(experiment / "evaluation_results"))
    assert report is not None
    assert report.attempts[0].score == 1.0


def test_recalculate_command_standardizes_cost_and_preserves_reported_cost(
    experiment: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """Explicit rates reprice token evidence and flow into experiment totals."""
    problem = Task.from_dir(tasks_root / "pyt").problem("01_step")
    evaluation_dir = experiment / "evaluation_results"
    request = Run.open(evaluation_dir).request()
    assert isinstance(request, Job)
    shutil.rmtree(evaluation_dir)
    plan(
        request.model_copy(
            update={
                "arms": (
                    request.arms[0],
                    request.arms[0].model_copy(update={"label": "other"}),
                )
            }
        ),
        evaluation_dir,
    )
    slot = complete_cell(experiment, problem.solution_dir("more-itertools"))
    evaluation = Run.open(evaluation_dir)
    launches = evaluation.launches()
    other = evaluation.slot(launches[1])
    shutil.copytree(
        slot.dir / "artifacts" / "workspace", other.dir / "artifacts" / "workspace"
    )
    seed_workspace_manifest(other)
    source_result = slot.result()
    assert source_result is not None
    other_result = source_result.model_copy(
        update={"trial_name": launches[1].trial_name, "trial_uri": other.dir.as_uri()}
    )
    other.dir.mkdir(parents=True, exist_ok=True)
    (other.dir / "result.json").write_text(other_result.model_dump_json(indent=2))
    shutil.copytree(slot.dir / "agent", other.dir / "agent")
    finalize(evaluation_dir)
    trajectory_path = slot.dir / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    trajectory["final_metrics"].update(
        total_prompt_tokens=2_000_000,
        total_cached_tokens=500_000,
        total_completion_tokens=250_000,
        total_cost_usd=0.75,
    )
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    trajectory_bytes = trajectory_path.read_bytes()
    result_path = slot.dir / "result.json"
    harbor_result = json.loads(result_path.read_text(encoding="utf-8"))
    harbor_result["agent_result"] = AgentContext(
        n_input_tokens=3_000_000,
        n_output_tokens=250_000,
        cost_usd=0.75,
    ).model_dump(mode="json")
    result_path.write_text(json.dumps(harbor_result), encoding="utf-8")
    other_result_path = other.dir / "result.json"
    other_document = json.loads(other_result_path.read_text(encoding="utf-8"))
    other_document["verifier_result"]["rewards"].update(
        pass_rate=0.5, passed=1, total=2
    )
    other_result_path.write_text(json.dumps(other_document), encoding="utf-8")
    previous = persisted_report(Run.open(evaluation_dir))
    assert previous is not None
    other_costs = previous.attempts[1].usage.model_dump(
        include={
            "cost_usd",
            "reported_cost_usd",
            "standardized_cost_usd",
            "input_cost_per_million",
            "output_cost_per_million",
            "cache_input_cost_per_million",
        }
    )

    result = CliRunner().invoke(
        app,
        [
            "recalculate",
            str(experiment),
            "--input-cost",
            "2",
            "--output-cost",
            "8",
            "--cache-input-cost",
            "1",
            "--implementor",
            "impl",
        ],
    )

    assert result.exit_code == 0, result.output
    report = persisted_report(Run.open(experiment / "evaluation_results"))
    assert report is not None
    usage = report.attempts[0].usage
    assert report.attempts[1].score == 0.25
    assert report.attempts[1].usage.model_dump(include=set(other_costs)) == other_costs
    assert usage.input_tokens == 2_000_000
    assert usage.uncached_input_tokens == 1_500_000
    assert usage.cache_input_tokens == 500_000
    assert usage.output_tokens == 250_000
    assert usage.reported_cost_usd == 0.75
    assert usage.standardized_cost_usd == usage.cost_usd == 5.5
    changed_result = json.loads(result_path.read_text(encoding="utf-8"))
    changed_result["verifier_result"]["rewards"].update(
        pass_rate=0.5, passed=1, total=2
    )
    result_path.write_text(json.dumps(changed_result), encoding="utf-8")

    unchanged_policy = CliRunner().invoke(app, ["recalculate", str(experiment)])

    assert unchanged_policy.exit_code == 0, unchanged_policy.output
    report = persisted_report(Run.open(experiment / "evaluation_results"))
    assert report is not None
    usage = report.attempts[0].usage
    assert report.attempts[0].score == 0.25
    assert usage.reported_cost_usd == 0.75
    assert usage.standardized_cost_usd == usage.cost_usd == 5.5

    repriced = CliRunner().invoke(
        app,
        [
            "recalculate",
            str(experiment),
            "--input-cost",
            "1",
            "--output-cost",
            "1",
            "--cache-input-cost",
            "1",
            "--implementor",
            "impl",
        ],
    )

    assert repriced.exit_code == 0, repriced.output
    report = persisted_report(Run.open(experiment / "evaluation_results"))
    assert report is not None
    usage = report.attempts[0].usage
    assert usage.reported_cost_usd == 0.75
    assert usage.standardized_cost_usd == usage.cost_usd == 2.25
    assert trajectory_path.read_bytes() == trajectory_bytes


def test_recalculate_command_rejects_an_incomplete_rate_policy(
    experiment: Path,
) -> None:
    """Rates without an implementor an experiment ran, or partial rates, write nothing.

    An experiment runs several implementors, so one rate set must name the
    implementor it prices, and that implementor must be one the experiment ran.
    """
    report_path = experiment / "ldb-result.json"
    original = report_path.read_bytes()
    rates = ["--input-cost", "2", "--output-cost", "8", "--cache-input-cost", "1"]

    missing_selector = CliRunner().invoke(app, ["recalculate", str(experiment), *rates])
    unknown_selector = CliRunner().invoke(
        app, ["recalculate", str(experiment), *rates, "--implementor", "missing"]
    )
    partial_rates = CliRunner().invoke(
        app, ["recalculate", str(experiment), "--input-cost", "1"]
    )

    assert missing_selector.exit_code == 2
    assert unknown_selector.exit_code == 2
    assert partial_rates.exit_code == 2
    assert report_path.read_bytes() == original


def test_recalculate_rejects_a_directory_without_a_typed_run(tmp_path: Path) -> None:
    """The CLI identifies a non-run path without rewriting anything."""
    result = CliRunner().invoke(app, ["recalculate", str(tmp_path)])

    assert result.exit_code == 2
    assert "Invalid value for path" in result.output
    assert not tuple(tmp_path.iterdir())


def test_recalculate_keeps_a_reward_whose_workspace_was_never_collected(
    experiment: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """A measured trial Harbor could not collect keeps the number it recorded.

    The container measured the application before collection failed, so its
    reward is the only measurement that trial will ever have, and there is no
    workspace to replay.
    """
    problem = Task.from_dir(tasks_root / "pyt").problem("01_step")
    slot = complete_cell(experiment, problem.solution_dir("more-itertools"))
    shutil.rmtree(slot.dir / "artifacts")
    recording_queue.batches.clear()

    rebuilt = recalculate(
        experiment, None, n_concurrent=1, environment=default_environment()
    )

    assert recording_queue.configs == ()
    assert json.loads((slot.dir / "verifier" / "reward.json").read_text()) == (
        STALE_REWARDS
    )
    assert rebuilt[1].report.attempts[0].reward == 0.4
    assert rebuilt[1].report.attempts[0].simplicity == 0.25


AUTHOR_MODEL = "fixture/author-model"


PRICED_MODEL = "fixture/priced-model"


UNPRICED_MODEL = "fixture/unpriced-model"


SEEDED_USAGE = AgentContext(
    n_input_tokens=2_000_000,
    n_cache_tokens=500_000,
    n_output_tokens=250_000,
    cost_usd=0.75,
)
"""Complete token evidence, so every seeded cell can be standardized."""


def _priced_experiment(tmp_path: Path, tasks_root: Path) -> Path:
    """Seed one experiment whose author and two implementors name three models.

    Every slot carries a graded result with complete token evidence and no
    collected workspace, so recalculation retains each reward and the only
    thing a pricing policy can change is what the cells cost.
    """
    task = Task.from_dir(tasks_root / "pyt")
    problem = task.problem("01_step")
    design_dir = tmp_path / "priced"
    plan(
        AuthorJob(
            tasks=(task,),
            agent=AgentConfig(name="oracle", model_name=AUTHOR_MODEL),
            n_concurrent=1,
        ),
        design_dir,
    )
    condition = AuthoredArtifact(
        task=task,
        source=problem.solution_dir("no-library").resolve(),
        attempt=1,
        problems=("01_step",),
    )
    evaluation_dir = design_dir / "evaluation_results"
    plan(
        Job(
            problems=(problem,),
            arms=tuple(
                Arm(
                    label=label,
                    condition=condition,
                    agent=AgentConfig(name="oracle", model_name=model),
                )
                for label, model in (
                    ("priced", PRICED_MODEL),
                    ("unpriced", UNPRICED_MODEL),
                )
            ),
            n_concurrent=1,
        ),
        evaluation_dir,
    )
    for run_dir in (design_dir, evaluation_dir):
        run = Run.open(run_dir)
        for launch in run.launches():
            _seed_priced_slot(run.slot(launch), launch)
        finalize(run_dir)
    return design_dir


def _seed_priced_slot(slot: Slot, launch: TrialLaunch) -> None:
    """Give one planned slot a graded Harbor result reporting its token usage."""
    slot.dir.mkdir(parents=True, exist_ok=True)
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
        agent_result=SEEDED_USAGE,
        verifier_result=VerifierResult(rewards=STALE_REWARDS),
    )
    (slot.dir / "result.json").write_text(result.model_dump_json(indent=2))


def _usage_by_implementor(report: RunReport) -> dict[str, UsageReport]:
    return {trial.implementor: trial.usage for trial in report.attempts}


def test_recalculate_pricing_config_reprices_every_model_it_names(
    tmp_path: Path, tasks_root: Path
) -> None:
    """One config reprices the author and each listed implementor in one pass.

    Rates belong to the model that ran a cell, not to the arm that mounted it,
    so a single invocation reaches the design run's authoring cell and one
    implementor's cell at their own rates, while the implementor the config
    never names keeps the cost its provider reported.
    """
    experiment = _priced_experiment(tmp_path, tasks_root)
    config = tmp_path / "pricing.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "models": {
                    AUTHOR_MODEL: {"input": 2, "output": 8, "cache_input": 1},
                    PRICED_MODEL: {"input": 4, "output": 16, "cache_input": 2},
                    "fixture/never-ran": {"input": 9, "output": 9, "cache_input": 9},
                }
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["recalculate", str(experiment), "--pricing-config", str(config)],
    )

    assert result.exit_code == 0, result.output
    design = persisted_report(Run.open(experiment))
    evaluation = persisted_report(Run.open(experiment / "evaluation_results"))
    assert design is not None and evaluation is not None
    author = _usage_by_implementor(design)["author"]
    assert author.reported_cost_usd == 0.75
    assert author.standardized_cost_usd == author.cost_usd == 5.5
    implementors = _usage_by_implementor(evaluation)
    assert implementors["priced"].reported_cost_usd == 0.75
    assert (
        implementors["priced"].standardized_cost_usd
        == implementors["priced"].cost_usd
        == 11.0
    )
    assert implementors["unpriced"].cost_usd == 0.75
    assert implementors["unpriced"].standardized_cost_usd is None
    assert implementors["unpriced"].input_cost_per_million is None


def test_recalculate_pricing_config_rejects_a_conflicting_or_idle_policy(
    tmp_path: Path, tasks_root: Path
) -> None:
    """A config that prices nothing, or duplicates the rate options, writes nothing.

    A model name is the whole selector, so combining a config with the single
    rate policy asks for two answers at once, and a config naming only models
    the target never ran would silently leave every cost as it was.
    """
    experiment = _priced_experiment(tmp_path, tasks_root)
    report_path = experiment / "ldb-result.json"
    original = report_path.read_bytes()
    config = tmp_path / "pricing.yaml"
    config.write_text(
        yaml.safe_dump(
            {"models": {PRICED_MODEL: {"input": 4, "output": 16, "cache_input": 2}}}
        ),
        encoding="utf-8",
    )
    absent = tmp_path / "absent.yaml"
    absent.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "fixture/never-ran": {"input": 1, "output": 1, "cache_input": 1}
                }
            }
        ),
        encoding="utf-8",
    )
    partial = tmp_path / "partial.yaml"
    partial.write_text(
        yaml.safe_dump({"models": {PRICED_MODEL: {"input": 4, "output": 16}}}),
        encoding="utf-8",
    )

    conflicting = CliRunner().invoke(
        app,
        [
            "recalculate",
            str(experiment),
            "--pricing-config",
            str(config),
            "--input-cost",
            "1",
        ],
    )
    idle = CliRunner().invoke(
        app,
        ["recalculate", str(experiment), "--pricing-config", str(absent)],
    )
    incomplete = CliRunner().invoke(
        app,
        ["recalculate", str(experiment), "--pricing-config", str(partial)],
    )

    assert conflicting.exit_code == 2
    assert idle.exit_code == 2
    assert incomplete.exit_code == 2
    assert report_path.read_bytes() == original


def test_recalculate_command_reprices_the_experiment_document(
    tasks_root: Path, tmp_path: Path, recording_queue: RecordingQueue
) -> None:
    """A repriced cell's standardized cost reaches the rebuilt experiment rows.

    The evaluation child owns the experiment document, so recalculating the
    experiment rebuilds it from both runs and displays it in place of the
    Evaluation Phase table.
    """
    runs = tmp_path / "runs"
    started = invoke_experiment(MINIMAL_CONFIG, tasks_root, runs)
    assert started.exit_code == 0, started.output
    experiment = experiment_dir(runs)
    evaluation = Run.open(experiment / "evaluation_results")
    result_path = evaluation.slot(evaluation.launches()[0]).dir / "result.json"
    document = json.loads(result_path.read_text(encoding="utf-8"))
    document["agent_result"] = SEEDED_USAGE.model_dump(mode="json")
    result_path.write_text(json.dumps(document), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "recalculate",
            str(experiment),
            "--input-cost",
            "2",
            "--output-cost",
            "8",
            "--cache-input-cost",
            "1",
            "--implementor",
            "impl",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Implementors" in result.output
    assert "library conditions" not in result.output
    rows = LdbResult.load(experiment / "ldb-result.json").trials
    assert [row.cost for row in rows if row.cost is not None] == [5.5]


def test_recalculate_command_replays_at_the_requested_concurrency(
    experiment: Path, tasks_root: Path, recording_queue: RecordingQueue
) -> None:
    """Remeasuring replays run where and how wide the command asks, not as saved."""
    problem = Task.from_dir(tasks_root / "pyt").problem("01_step")
    complete_cell(experiment, problem.solution_dir("more-itertools"))
    recording_queue.batches.clear()
    recording_queue.concurrencies.clear()

    result = CliRunner().invoke(
        app, ["recalculate", str(experiment), "environment.type=docker", "-n", "3"]
    )

    assert result.exit_code == 0, result.output
    assert recording_queue.concurrencies == [3]
    (replayed,) = recording_queue.configs
    assert replayed.environment.type is not None
    assert replayed.environment.type.value == "docker"
