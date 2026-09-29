"""Shared fixtures for lib-design-bench tests.

The real task fixtures live on disk under `tests/fixtures/tasks/`. The
fixtures in this module only resolve paths and construct config objects —
they never invent yaml at runtime, so tests run against the same task
shape the implementer will use in production.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_FIXTURES_ROOT = Path(__file__).parent / "fixtures"
_TASKS_ROOT = _FIXTURES_ROOT / "tasks"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register opt-in execution for the costly full Harbor workflows."""
    group = parser.getgroup("lib-design-bench")
    group.addoption(
        "--run-slow-e2e",
        action="store_true",
        default=False,
        help="run the slow end-to-end Docker workflows",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip Docker work only for a missing daemon, and slow flows by opt-in."""
    docker_items = [item for item in items if item.get_closest_marker("docker")]
    docker_available = _docker_daemon_available() if docker_items else True
    run_slow_e2e = config.getoption("--run-slow-e2e")
    for item in items:
        if item.get_closest_marker("docker") and not docker_available:
            item.add_marker(pytest.mark.skip(reason="Docker daemon is unavailable"))
        if item.get_closest_marker("slow_e2e") and not run_slow_e2e:
            item.add_marker(
                pytest.mark.skip(reason="slow e2e tests require --run-slow-e2e")
            )


def _docker_daemon_available() -> bool:
    """Return whether Docker's daemon, rather than a particular image, is ready."""
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@pytest.fixture
def fixtures_root() -> Path:
    """Absolute path to `tests/fixtures/`, including environment-only fixtures."""
    return _FIXTURES_ROOT


@pytest.fixture
def tasks_root() -> Path:
    """Absolute path to the checked-in `tests/fixtures/tasks/` root.

    Two tasks live there:
      - `pyt` : python, 2 Evaluation Phase problems
      - `rsj` : rust, 2 Evaluation Phase problems
    """
    return _TASKS_ROOT
