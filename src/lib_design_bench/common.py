"""Shared helpers for CLI command modules."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Literal

import structlog

TIMESTAMP_FORMAT = "%Y-%m-%d__%H-%M-%S"
"""Harbor's default job name: the local time a job starts."""
_MAX_HARBOR_TRIAL_NAME_LENGTH = 58
_MAX_EVALUATION_PROBLEM_PREFIX_LENGTH = 16
_TRIAL_HASH_WIDTH = 8
_TASKS_REPO_URL = "https://github.com/gabeorlanski/ldb-tasks.git"
_TASKS_REVISION = "a1f4e886063373d43e52b53b98492cf108f294e3"
logger = structlog.get_logger(__name__)
ReasoningLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def safe_path_part(value: str) -> str:
    """Return `value` sanitized for use as one path segment."""
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    return safe.strip(".-_") or "x"


def harbor_trial_name(identity: str) -> str:
    """Return a trial identity whose Harbor agent environment stays within 63 chars."""
    safe = _harbor_trial_component(identity, fallback="trial")
    if safe == identity and len(safe) <= _MAX_HARBOR_TRIAL_NAME_LENGTH:
        return safe
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    suffix = f"__{digest}"
    prefix = safe[: _MAX_HARBOR_TRIAL_NAME_LENGTH - len(suffix)].rstrip("-_")
    return f"{prefix or 'trial'}{suffix}"


def evaluation_trial_name(
    *,
    task: str,
    setup: str,
    problem: str,
    attempt: int,
    agent_name: str,
    implementor_label: str,
    run_timestamp: datetime,
) -> str:
    """Return the persisted name for one Evaluation Phase trial."""
    timestamp = run_timestamp.astimezone(UTC).isoformat(timespec="microseconds")
    identity = [
        task,
        setup,
        str(attempt),
        agent_name,
        implementor_label,
        problem,
        timestamp,
    ]
    identity_hash = hashlib.sha256("\0".join(identity).encode()).hexdigest()[
        :_TRIAL_HASH_WIDTH
    ]
    return (
        f"{_harbor_trial_component(task)}-{_harbor_trial_component(setup)}"
        f"-a{attempt}"
        f"__{_harbor_trial_component(agent_name)[:_MAX_EVALUATION_PROBLEM_PREFIX_LENGTH]}"
        f"__{_harbor_trial_component(problem)[:_MAX_EVALUATION_PROBLEM_PREFIX_LENGTH]}"
        f"__{identity_hash}"
    )


def _harbor_trial_component(value: str, *, fallback: str = "x") -> str:
    """Return a lowercased Harbor-safe trial-name component without truncating it."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower()).strip("-_") or fallback


def get_repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parents[2]


def get_tasks_dir() -> Path:
    """Return the cached ldb-tasks checkout, fetching the pinned revision if needed.

    A first use fetches into a temporary directory and renames it into place, so
    a failed download leaves nothing behind at the cache path.
    """
    tasks_dir = Path.home() / ".cache" / "lib-design-bench" / "tasks"
    if not tasks_dir.exists():
        tasks_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tasks_dir.parent) as staging_root:
            staging = Path(staging_root) / "tasks"
            subprocess.run(["git", "init", "--quiet", str(staging)], check=True)
            _checkout_tasks_revision(staging)
            staging.rename(tasks_dir)
        return tasks_dir
    head = subprocess.run(
        ["git", "-C", str(tasks_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != _TASKS_REVISION:
        _checkout_tasks_revision(tasks_dir)
    return tasks_dir


def _checkout_tasks_revision(repo: Path) -> None:
    """Fetch only the pinned ldb-tasks commit into `repo` and check it out."""
    logger.info("Fetching ldb-tasks", revision=_TASKS_REVISION, path=str(repo))
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "fetch",
            "--depth",
            "1",
            _TASKS_REPO_URL,
            _TASKS_REVISION,
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "advice.detachedHead=false",
            "checkout",
            "--quiet",
            "--detach",
            _TASKS_REVISION,
        ],
        check=True,
    )


def hash_paths(paths: tuple[Path, ...]) -> str:
    """Return a stable content hash for files under `paths`."""
    digest = hashlib.sha256()
    for root in sorted(path.resolve() for path in paths):
        digest.update(root.as_posix().encode())
        if root.is_file():
            digest.update(root.read_bytes())
            continue
        for child in sorted(path for path in root.rglob("*") if path.is_file()):
            digest.update(child.relative_to(root).as_posix().encode())
            digest.update(child.read_bytes())
    return digest.hexdigest()


def now_timestamp() -> str:
    """Return the current local time as Harbor names a default job."""
    return datetime.now().strftime(TIMESTAMP_FORMAT)
