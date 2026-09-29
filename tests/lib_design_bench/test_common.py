import subprocess
from pathlib import Path

import pytest

from lib_design_bench import common


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return its stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    """Commit one new file named `name` and return the commit hash."""
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def tasks_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the task cache at a temporary home and a local ldb-tasks remote."""
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-q")
    _git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(common, "_TASKS_REPO_URL", remote.as_uri())
    return remote


def test_tasks_dir_clones_the_pinned_revision_on_first_use(
    tasks_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing cache clones exactly the pinned commit and leaves no staging dir."""
    pinned = _commit(tasks_repo, "pinned")
    _commit(tasks_repo, "later")
    monkeypatch.setattr(common, "_TASKS_REVISION", pinned)

    tasks_dir = common.get_tasks_dir()

    assert tasks_dir == Path.home() / ".cache" / "lib-design-bench" / "tasks"
    assert _git(tasks_dir, "rev-parse", "HEAD") == pinned
    assert (tasks_dir / "pinned").is_file()
    assert not (tasks_dir / "later").exists()
    assert [path.name for path in tasks_dir.parent.iterdir()] == ["tasks"]


def test_tasks_dir_moves_a_cached_checkout_to_a_new_pin(
    tasks_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached checkout at an older pin is moved to the current pin."""
    monkeypatch.setattr(common, "_TASKS_REVISION", _commit(tasks_repo, "old"))
    common.get_tasks_dir()
    updated = _commit(tasks_repo, "new")
    monkeypatch.setattr(common, "_TASKS_REVISION", updated)

    tasks_dir = common.get_tasks_dir()

    assert _git(tasks_dir, "rev-parse", "HEAD") == updated
    assert (tasks_dir / "new").is_file()
