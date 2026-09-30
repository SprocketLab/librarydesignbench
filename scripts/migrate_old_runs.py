"""Copy ../lib-design-bench runs, pruned, with the minimal `ldb-result.json`.

Usage:
  uv run python scripts/migrate_old_runs.py --output DIR [--old-repo PATH]
      [--dry-run] [RUN ...]

Each RUN is a path relative to the old repo: a run, or a directory holding
runs. Without any, every `runs/...` path the old repo's AGENTS.md names is
migrated. An experiment's `evaluation_results` child belongs to its root, so
each run root is copied to DIR/<path under runs>/ with one `ldb-result.json`,
whose paths resolve inside that copy. The original runs are only read.

Outcome classes come from the old repo's own classifier, run in its
environment: the current one expects verifier-owned static measurements that
old trials never recorded. One override applies: a trial that failed on
Portkey counts as a finished, failed trial rather than one owed a rerun.

The copy leaves out:
  the old run-level LDB result documents (ldb-result.json and
    experiment-result.json),
  .harbor-* directories (superseded trial evidence and Harbor scratch),
  runs saved inside a run, such as ad-hoc `ldb verify run` replays,
  ELF core dumps,
  target/, node_modules/, __pycache__/ and .venv/ under trial artifacts,
  every entry of a trial's agent/ directory except trajectory.json.
Harbor's job result.json at each run root, and every trial's own result.json
and trial-report.json, are copied unchanged.
`--dry-run` writes nothing, and reports what the copy would hold and leave out.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from harbor.models.trial.config import AgentConfig

from lib_design_bench.models.reports import RESULT_FILE_NAME
from lib_design_bench.models.reports import AgentDetails
from lib_design_bench.models.reports import LdbResult
from lib_design_bench.models.reports import LibraryResult
from lib_design_bench.models.reports import OutcomeClass
from lib_design_bench.models.reports import ResultMeta
from lib_design_bench.models.reports import TrialMetric
from lib_design_bench.reports.results import agent_details

OUTCOMES = ("finished", "reanalyze", "reverify", "rerun")
BUILD_DIRS = frozenset({"target", "node_modules", "__pycache__", ".venv"})
OLD_RESULTS = frozenset({"ldb-result.json", "experiment-result.json"})

CLASSIFY = """
import json, sys
from pathlib import Path
from lib_design_bench.evaluation.outcomes import classify_run
from lib_design_bench.evaluation.run_dir import Run
outcomes = {d: classify_run(Run.open(Path(d))) for d in sys.argv[2:]}
Path(sys.argv[1]).write_text(json.dumps(outcomes), encoding="utf-8")
"""
"""Runs in the old repo's environment: that repo's classifier decided its runs."""


def main() -> None:
    """Copy every selected run that has a saved result, with its new result."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("runs", nargs="*", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--old-repo",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "lib-design-bench",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    old_repo = args.old_repo.resolve()
    runs_dir = old_repo / "runs"
    selected = args.runs or [
        Path(path)
        for path in re.findall(
            r"`(runs/[^`]+)`", (old_repo / "AGENTS.md").read_text(encoding="utf-8")
        )
    ]
    roots = [root for path in selected for root in run_roots(old_repo / path)]
    migratable = []
    for root in dict.fromkeys(roots):
        if (root / "ldb-result.json").is_file():
            migratable.append(root)
        else:
            print(f"skip (never finalized): {root.relative_to(runs_dir)}")
    outcomes = classify(old_repo, migratable)
    results = {root: migrate(root, outcomes) for root in migratable}
    destinations = {root: args.output / root.relative_to(runs_dir) for root in results}
    if existing := [d for d in destinations.values() if d.exists()]:
        raise FileExistsError(f"Refusing to overwrite: {existing}")
    copies: list[tuple[Path, Path]] = []
    kept_bytes = 0
    left: Counter[str] = Counter()
    for root, result in results.items():
        kept, dropped = plan_copy(root)
        copies.extend(
            (path, destinations[root] / path.relative_to(root)) for path in kept
        )
        kept_bytes += sum(path.lstat().st_size for path in kept)
        left.update(dropped)
        print(
            f"{destinations[root]}: {len(result.trials)} trials, "
            f"{len(result.libraries)} libraries, complete={result.meta.complete}"
        )
    if not args.dry_run:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(copy_entry, copies))
        # Each result is written last, so a present result means a whole copy.
        for root, result in results.items():
            (destinations[root] / RESULT_FILE_NAME).write_text(
                result.to_json(), encoding="utf-8"
            )
    copied, left_out = (
        ("would copy", "would leave out") if args.dry_run else ("copied", "left out")
    )
    print(f"{copied} {kept_bytes / 2**30:7.2f} GiB in {len(copies)} entries")
    for category, size in left.most_common():
        print(f"{left_out} {size / 2**30:7.2f} GiB  {category}")


def run_roots(path: Path) -> list[Path]:
    """Return the run roots at or below `path`, without their child runs."""
    roots = []
    for dirpath, dirnames, filenames in os.walk(path):
        here = Path(dirpath)
        if "manifest.json" in filenames and "request" in read(here / "manifest.json"):
            roots.append(here)
            dirnames.clear()
            continue
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".harbor-")
            and name not in ("agent", "artifacts", "verifier")
        )
    if not roots:
        raise ValueError(f"No run under {path}")
    return roots


def classify(old_repo: Path, roots: list[Path]) -> dict[Path, dict[str, OutcomeClass]]:
    """Classify every run and experiment child with the old repo's classifier."""
    run_dirs = [
        run_dir
        for root in roots
        for run_dir in (root, root / "evaluation_results")
        if (run_dir / "manifest.json").is_file()
    ]
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "outcomes.json"
        subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(old_repo),
                "python",
                "-c",
                CLASSIFY,
                str(output),
                *map(str, run_dirs),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            # The old repo's environment, not the one running this script.
            env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"},
        )
        outcomes = {Path(key): value for key, value in read(output).items()}
    for run_dir, classes in outcomes.items():
        for name, outcome in classes.items():
            exception = slot_result(run_dir, name).get("exception_info") or {}
            message = (exception.get("exception_message") or "").split("\n")[0]
            if outcome == "rerun" and "Portkey" in message:
                classes[name] = "finished"
    return outcomes


def migrate(root: Path, outcomes: dict[Path, dict[str, OutcomeClass]]) -> LdbResult:
    """Project one old run root's saved documents onto the current result."""
    manifest = read(root / "manifest.json")
    request = manifest["request"]
    is_experiment = manifest.get("experiment") is not None
    match request["kind"]:
        case "author":
            child = root / "evaluation_results"
            evaluation_dir = child if (child / "manifest.json").is_file() else None
            if evaluation_dir is not None and not is_experiment:
                raise ValueError(f"Design run with an evaluation child: {root}")
            run_dirs = [root] if evaluation_dir is None else [root, evaluation_dir]
        case "job":
            evaluation_dir = root
            run_dirs = [root]
        case kind:
            raise ValueError(f"Unsupported run kind {kind}: {root}")
    saved = {run_dir: attempts(run_dir, outcomes) for run_dir in run_dirs}

    libraries: dict[str, LibraryResult] = {}
    if request["kind"] == "author":
        author = agent_details(AgentConfig.model_validate(request["agent"]))
    for attempt in saved[root] if request["kind"] == "author" else ():
        slot = root / "design_results" / attempt["trial_name"]
        libraries[f"{attempt['problem']}/a{attempt['attempt']}"] = LibraryResult(
            type="authored",
            task=attempt["problem"],
            path=os.path.relpath(workspace(slot), root),
            attempt=attempt["attempt"],
            author=author,
            score=attempt["score"] if attempt["incomplete_reason"] is None else None,
            incomplete_reason=attempt["incomplete_reason"],
            **author_usage(slot, attempt["usage"]),
        )

    implementors: dict[str, AgentDetails] = {}
    trials: list[TrialMetric] = []
    if evaluation_dir is not None:
        for arm in read(evaluation_dir / "manifest.json")["request"]["arms"]:
            details = agent_details(AgentConfig.model_validate(arm["agent"]))
            if implementors.setdefault(arm["label"], details) != details:
                raise ValueError(f"Implementor {arm['label']} has two agents: {root}")
        for attempt in saved[evaluation_dir]:
            task = attempt["problem"]
            match attempt["library_kind"]:
                case "agent":
                    key = f"{task}/a{attempt['design_attempt']}"
                    if key not in libraries:
                        raise ValueError(
                            f"{attempt['trial_name']} uses a library its "
                            f"experiment did not author: {root}"
                        )
                case "existing":
                    key = f"{task}/existing/{attempt['library']}"
                    libraries.setdefault(key, LibraryResult(type="existing", task=task))
                case "no-library":
                    key = f"{task}/no-library"
                    libraries.setdefault(
                        key, LibraryResult(type="no-library", task=task)
                    )
                case kind:
                    raise ValueError(f"Unknown library kind {kind}: {root}")
            outcome = outcomes[evaluation_dir][attempt["trial_name"]]
            trials.append(
                TrialMetric(
                    name=attempt["trial_name"],
                    task=task,
                    attempt=attempt["attempt"],
                    problem=attempt["example"],
                    library=key,
                    implementor=attempt["implementor"],
                    outcome=outcome,
                    simplicity=attempt["compaction"],
                    pass_rate=attempt["pass_rate"],
                    score=attempt["score"],
                    sandbox_cost=attempt["sandbox_usage"]["cost_usd"],
                    incomplete_reason=attempt["incomplete_reason"],
                    **usage(attempt["usage"]),
                )
            )

    classes = [outcome for d in run_dirs for outcome in outcomes[d].values()]
    complete = all(outcome == "finished" for outcome in classes) and (
        not is_experiment or bool(trials)
    )
    finished = [
        result.get("finished_at")
        for run_dir in run_dirs
        for name in outcomes[run_dir]
        for result in (slot_result(run_dir, name),)
    ]
    prompts = [
        path
        for run_dir in run_dirs
        for attempt in saved[run_dir]
        if (prompt := attempt["prompt_path"]) is not None
        for path in (os.path.relpath(run_dir / prompt, root),)
    ]
    return LdbResult(
        schema_version=1,
        id=root.name,
        meta=ResultMeta(
            type="experiment"
            if is_experiment
            else "design"
            if request["kind"] == "author"
            else "evaluation",
            started_at=read(root / "ldb-config.json")["started_at"],
            # Verification can happen long after the agent execution; only a
            # complete run whose every trial is dated has a finish time.
            finished_at=max(datetime.fromisoformat(at) for at in finished if at)
            if complete and finished and all(finished)
            else None,
            repo_commit=manifest["repo_commit"],
            tasks_hash=manifest["problems_hash"],
            prompt_paths=tuple(dict.fromkeys(prompts)),
            complete=complete,
            outcome_counts={name: classes.count(name) for name in OUTCOMES},
        ),
        implementors=implementors,
        libraries=libraries,
        trials=tuple(trials),
    )


def attempts(
    run_dir: Path, outcomes: dict[Path, dict[str, OutcomeClass]]
) -> list[dict[str, Any]]:
    """Return a run's saved trial reports, which must cover exactly its slots."""
    saved = read(run_dir / "ldb-result.json")["attempts"]
    if sorted(attempt["trial_name"] for attempt in saved) != sorted(outcomes[run_dir]):
        raise ValueError(f"Saved report disagrees with the planned slots: {run_dir}")
    return saved


def usage(document: dict[str, Any]) -> dict[str, Any]:
    """Project one old usage report onto the result's agent-spend fields."""
    return {
        "input_tokens": document.get("input_tokens"),
        "output_tokens": document.get("output_tokens"),
        "input_cache_tokens": document.get("cache_input_tokens"),
        "elapsed": document.get("time_spent"),
        "steps": document.get("agent_steps"),
        "cost": document.get("cost_usd"),
    }


def author_usage(slot: Path, document: dict[str, Any]) -> dict[str, Any]:
    """Project one author's usage with every Codex session it ran.

    A Codex trial report records only the last session it saw, which is a
    subagent's whenever the author spawned any. When the slot kept its session
    rollouts, each session's final token totals are summed and priced at the
    report's rates; a slot without rollouts ran one session, which its report
    already covers.
    """
    finals = [
        totals[-1]
        for rollout in sorted((slot / "agent" / "sessions").rglob("rollout-*.jsonl"))
        if (
            totals := [
                event["payload"]["info"]["total_token_usage"]
                for event in map(json.loads, rollout.read_text().splitlines())
                if event["type"] == "event_msg"
                and event["payload"]["type"] == "token_count"
                and event["payload"].get("info")
            ]
        )
    ]
    if not finals:
        return usage(document)
    input_tokens = sum(final["input_tokens"] for final in finals)
    cached = sum(final["cached_input_tokens"] for final in finals)
    output_tokens = sum(final["output_tokens"] for final in finals)
    cost = (
        (input_tokens - cached) * document["input_cost_per_million"]
        + cached * document["cache_input_cost_per_million"]
        + output_tokens * document["output_cost_per_million"]
    ) / 1e6
    return {
        **usage(document),
        "input_tokens": float(input_tokens),
        "output_tokens": float(output_tokens),
        "input_cache_tokens": float(cached),
        "cost": cost,
    }


def workspace(slot: Path) -> Path:
    """Return where an author slot's collected workspace lives."""
    manifest = slot / "artifacts" / "manifest.json"
    if manifest.is_file():
        for entry in read(manifest):
            if entry["source"] == "/workspace":
                return slot / entry["destination"]
    return slot / "artifacts" / "workspace"


def slot_result(run_dir: Path, name: str) -> dict[str, Any]:
    """Return one slot's Harbor result document, or nothing when it never ran."""
    for path in (run_dir / "design_results" / name, run_dir / name):
        if (path / "result.json").is_file():
            return read(path / "result.json")
    return {}


def plan_copy(root: Path) -> tuple[list[Path], Counter[str]]:
    """Return the entries a pruned copy keeps, and the bytes it leaves out."""
    kept: list[Path] = []
    left: Counter[str] = Counter()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dropped = junk(root, here, [*dirnames, *filenames])
        for name, category in dropped.items():
            left[category] += disk_usage(here / name)
        linked = [name for name in dirnames if (here / name).is_symlink()]
        dirnames[:] = [
            name for name in dirnames if name not in dropped and name not in linked
        ]
        kept.extend(
            here / name for name in [*filenames, *linked] if name not in dropped
        )
    return kept, left


def junk(root: Path, here: Path, names: list[str]) -> dict[str, str]:
    """Name the entries of one directory a pruned copy leaves out, by category."""
    is_run_dir = here in (root, root / "evaluation_results")
    is_agent_dir = here.name == "agent" and (here.parent / "config.json").is_file()
    in_artifacts = "artifacts" in here.relative_to(root).parts
    dropped = {}
    for name in names:
        path = here / name
        if name.startswith(".harbor-"):
            dropped[name] = ".harbor-* directories"
        elif (
            path != root / "evaluation_results"
            and (path / "ldb-config.json").is_file()
            and (path / "ldb-result.json").is_file()
        ):
            # A finalized run, not a manual reset's snapshot of run documents.
            dropped[name] = "runs saved inside a run"
        elif is_run_dir and name in OLD_RESULTS:
            dropped[name] = "old run-level result documents"
        elif is_agent_dir and name != "trajectory.json":
            dropped[name] = "agent files other than trajectory.json"
        elif in_artifacts and name in BUILD_DIRS and path.is_dir():
            dropped[name] = "build and dependency caches"
        elif (
            re.fullmatch(r"core(\.\d+)?", name)
            and path.is_file()
            and is_core_dump(path)
        ):
            dropped[name] = "core dumps"
    return dropped


def copy_entry(copy: tuple[Path, Path]) -> None:
    """Copy one file, or one symlink as a symlink, creating its directory."""
    source, target = copy
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, target)


def is_core_dump(path: Path) -> bool:
    """Return whether a file is an ELF core image rather than a same-named source."""
    if path.is_symlink():
        return False
    with path.open("rb") as file:
        header = file.read(18)
    return (
        len(header) == 18
        and header[:4] == b"\x7fELF"
        and int.from_bytes(header[16:18], "little" if header[5] == 1 else "big") == 4
    )


def disk_usage(path: Path) -> int:
    """Return the bytes one file or directory tree occupies."""
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    return sum(
        (Path(dirpath) / name).lstat().st_size
        for dirpath, _dirnames, filenames in os.walk(path)
        for name in filenames
    )


def read(path: Path) -> Any:
    """Load one JSON document."""
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
