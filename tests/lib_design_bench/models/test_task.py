from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lib_design_bench.models import Task
from lib_design_bench.models.task import Problem


def _write_design_task(task_dir: Path) -> None:
    """Give malformed-task tests a valid Design Phase Harbor task."""
    design = task_dir / "design"
    design.mkdir(parents=True, exist_ok=True)
    (design / "task.toml").write_text(
        'schema_version = "1.4"\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        "[agent]\ntimeout_sec = 120.0\n\n"
        "[environment]\nbuild_timeout_sec = 120.0\n\n"
        'artifacts = [{ source = "/workspace", destination = "workspace" }]\n',
        encoding="utf-8",
    )
    (design / "instruction.md").write_text("Build the library.\n", encoding="utf-8")


def test_task_resolves_declared_problems_and_serializes_as_its_directory(
    tasks_root: Path,
) -> None:
    """A persisted task is reconstructed from its source directory."""
    task = Task.from_dir(tasks_root / "pyt")

    assert task.problem("01_step") == Problem(task=task, name="01_step")
    assert task.active_problems() == tuple(
        Problem(task=task, name=name) for name in task.problems
    )
    assert Task.model_validate_json(task.model_dump_json()) == task

    with pytest.raises(ValueError, match="not declared"):
        task.problem("missing")


def test_task_rejects_harbor_steps_in_its_design_task(
    tasks_root: Path, tmp_path: Path
) -> None:
    """The Design Phase Harbor task is one library attempt, never a step sequence."""
    task_dir = tmp_path / "pyt"
    shutil.copytree(tasks_root / "pyt", task_dir, symlinks=True)
    task_path = task_dir / "design" / "task.toml"
    task_path.write_text(
        task_path.read_text(encoding="utf-8")
        + """
[[steps]]
name = "design"
min_reward = 0.0
artifacts = [{ source = "/tmp", destination = "step" }]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not support Harbor steps"):
        Task.from_dir(task_dir)


def test_task_requires_a_declared_spine_for_existing_libraries(
    tasks_root: Path, tmp_path: Path
) -> None:
    """Every comparator-bearing task names one existing library as its spine."""
    task_dir = tmp_path / "pyt"
    shutil.copytree(tasks_root / "pyt", task_dir, symlinks=True)
    path = task_dir / "task.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("spine: more-itertools\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires a spine"):
        Task.from_dir(task_dir)

    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "language: python\n", "language: python\nspine: nonexistent\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not a declared existing library"):
        Task.from_dir(task_dir)


@pytest.mark.parametrize(
    ("manifest", "error"),
    (
        (None, "must contain Cargo.toml"),
        (
            '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\nautobins = false\n',
            "executable target",
        ),
    ),
)
def test_rust_active_problems_require_parseable_executable_starters(
    tmp_path: Path, tasks_root: Path, manifest: str | None, error: str
) -> None:
    """Active Rust problems must carry a valid application starter in source."""
    task_dir = tmp_path / "rsj"
    shutil.copytree(tasks_root / "rsj", task_dir)
    problem_dir = task_dir / "evaluation" / "01_step"
    workspace = problem_dir / "workspace"
    if manifest is not None:
        (workspace / "Cargo.toml").write_text(manifest, encoding="utf-8")
    else:
        (workspace / "Cargo.toml").unlink()

    with pytest.raises(ValueError, match=error):
        Task.from_dir(problem_dir.parents[1])


def test_rust_environment_requires_exact_versions(
    tmp_path: Path,
) -> None:
    """Rust environments cannot rely on a floating Cargo resolver version."""
    task_dir = tmp_path / "rust-task"
    environment_dir = task_dir / "environment"
    environment_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        "library_name: rust-task\nlanguage: rust\nproblems:\n  - 01_problem\n",
        encoding="utf-8",
    )
    _write_design_task(task_dir)
    problem_dir = task_dir / "evaluation" / "01_problem"
    problem_dir.mkdir(parents=True)
    (problem_dir / "task.toml").write_text(
        'schema_version = "1.2"\n\n[task]\nname = "rust/problem"\n'
        'description = "Fixture."\nauthors = []\nkeywords = []\n\n'
        '[metadata]\nauthor_name = "tests"\nauthor_email = "unknown"\n'
        'difficulty_explanation = "Fixture."\nsolution_explanation = "Fixture."\n'
        "tags = []\nexpert_time_estimate_hours = 0.1\n\n"
        "[verifier]\ntimeout_sec = 60.0\n\n"
        "[agent]\ntimeout_sec = 120.0\n\n"
        "[environment]\nbuild_timeout_sec = 120.0\n",
        encoding="utf-8",
    )
    (environment_dir / "Cargo.toml").write_text(
        '[package]\nname = "seed"\nversion = "0.0.1"\n\n'
        '[dependencies]\nserde_json = "1"\n',
        encoding="utf-8",
    )
    (environment_dir / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (environment_dir / "Dockerfile").write_text("FROM rust:1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must use an exact version"):
        Task.from_dir(task_dir)


def test_task_requires_each_active_problem_directory(tmp_path: Path) -> None:
    """Task loading rejects selected problems that are absent on disk."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    environment_dir = task_dir / "environment"
    environment_dir.mkdir()
    (environment_dir / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (environment_dir / "requirements.txt").write_text(
        "pytest==9.1.1\n", encoding="utf-8"
    )
    (task_dir / "task.yaml").write_text(
        "library_name: example\nlanguage: python\nproblems:\n  - missing\n",
        encoding="utf-8",
    )
    _write_design_task(task_dir)

    with pytest.raises(ValueError, match="Active problem directory does not exist"):
        Task.from_dir(task_dir)


def test_task_rejects_a_comparator_dependency_in_its_base_environment(
    tmp_path: Path,
) -> None:
    """The shared base image cannot give Design Phase or floor agents a ceiling package."""
    task_dir = tmp_path / "task"
    environment_dir = task_dir / "environment"
    problem_dir = task_dir / "evaluation" / "problem"
    library_dir = task_dir / "existing_library" / "measured"
    environment_dir.mkdir(parents=True)
    problem_dir.mkdir(parents=True)
    library_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        """
library_name: example
language: python
problems:
  - problem
spine: measured
existing_libraries:
  measured: existing_library/measured
""".lstrip(),
        encoding="utf-8",
    )
    _write_design_task(task_dir)
    (problem_dir / "task.toml").write_text(
        'schema_version = "1.4"\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        "[agent]\ntimeout_sec = 120.0\n\n"
        "[environment]\nbuild_timeout_sec = 120.0\n",
        encoding="utf-8",
    )
    (library_dir / "config.yaml").write_text(
        """
source: measured==1.0.0
clone: https://example.test/measured.git
ref: v1.0.0
isolation_identifiers: [measured]
""".lstrip(),
        encoding="utf-8",
    )
    (environment_dir / "requirements.txt").write_text(
        "measured==1.0.0\n",
        encoding="utf-8",
    )
    (environment_dir / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")

    with pytest.raises(ValueError, match="preinstalls comparator dependencies"):
        Task.from_dir(task_dir)


def test_haskell_environment_dependencies_include_ghc_boot_libraries(
    tmp_path: Path,
) -> None:
    """Haskell prompts list the libraries GHC ships, not only the Cabal manifest."""
    task_dir = tmp_path / "task"
    environment_dir = task_dir / "environment"
    problem_dir = task_dir / "evaluation" / "problem"
    environment_dir.mkdir(parents=True)
    problem_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        "library_name: example\nlanguage: haskell\nproblems:\n  - problem\n",
        encoding="utf-8",
    )
    _write_design_task(task_dir)
    (problem_dir / "task.toml").write_text(
        'schema_version = "1.4"\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        "[agent]\ntimeout_sec = 120.0\n\n"
        "[environment]\nbuild_timeout_sec = 120.0\n",
        encoding="utf-8",
    )
    (environment_dir / "Dockerfile").write_text(
        "FROM haskell:9.8.4-slim AS ghc\nFROM debian:bookworm-slim\n",
        encoding="utf-8",
    )
    (environment_dir / "environment.cabal").write_text(
        "name: environment\nversion: 0.0.1\nlibrary\n  build-depends:\n"
        "      base\n    , vector\n  default-language: Haskell2010\n",
        encoding="utf-8",
    )
    (environment_dir / "cabal.project.freeze").write_text("", encoding="utf-8")

    dependencies = set(Task.from_dir(task_dir).environment_dependencies)

    assert {"base", "vector", "containers", "stm", "text", "mtl"} <= dependencies
    assert "ghc" not in dependencies
    assert "Haskell2010" not in dependencies
    assert "parsec" not in dependencies


def test_haskell_environment_requires_a_named_ghc_stage(tmp_path: Path) -> None:
    """A Haskell Dockerfile without an `AS ghc` stage cannot advertise boot libraries."""
    task_dir = tmp_path / "task"
    environment_dir = task_dir / "environment"
    problem_dir = task_dir / "evaluation" / "problem"
    environment_dir.mkdir(parents=True)
    problem_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        "library_name: example\nlanguage: haskell\nproblems:\n  - problem\n",
        encoding="utf-8",
    )
    _write_design_task(task_dir)
    (problem_dir / "task.toml").write_text(
        'schema_version = "1.4"\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        "[agent]\ntimeout_sec = 120.0\n\n"
        "[environment]\nbuild_timeout_sec = 120.0\n",
        encoding="utf-8",
    )
    (environment_dir / "Dockerfile").write_text(
        "FROM haskell:9.10.3 AS fourmolu\nFROM debian:bookworm-slim\n",
        encoding="utf-8",
    )
    (environment_dir / "environment.cabal").write_text(
        "name: environment\nversion: 0.0.1\nbuild-depends: base\n", encoding="utf-8"
    )
    (environment_dir / "cabal.project.freeze").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="AS ghc"):
        Task.from_dir(task_dir)


def test_rust_environment_advertises_real_crate_names_behind_renames(
    tmp_path: Path,
) -> None:
    """A `package = ...` rename is listed by the crate an author must actually name."""
    task_dir = tmp_path / "rust-task"
    environment_dir = task_dir / "environment"
    environment_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        "library_name: rust-task\nlanguage: rust\nproblems:\n  - 01_problem\n",
        encoding="utf-8",
    )
    _write_design_task(task_dir)
    problem_dir = task_dir / "evaluation" / "01_problem"
    problem_dir.mkdir(parents=True)
    (problem_dir / "task.toml").write_text(
        'schema_version = "1.4"\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        "[agent]\ntimeout_sec = 120.0\n\n"
        "[environment]\nbuild_timeout_sec = 120.0\n",
        encoding="utf-8",
    )
    (environment_dir / "Cargo.toml").write_text(
        '[package]\nname = "seed"\nversion = "0.0.1"\n\n[dependencies]\n'
        'serde_json = "=1.0.151"\nsyn-3 = { package = "syn", version = "=3.0.3" }\n',
        encoding="utf-8",
    )
    (environment_dir / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (environment_dir / "Dockerfile").write_text("FROM rust:1\n", encoding="utf-8")
    starter = problem_dir / "workspace"
    (starter / "src").mkdir(parents=True)
    (starter / "Cargo.toml").write_text(
        '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (starter / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")

    dependencies = Task.from_dir(task_dir).environment_dependencies

    assert dependencies == ("serde_json", "syn")


def test_task_rejects_a_haskell_comparator_prefetched_by_its_dockerfile(
    tmp_path: Path,
) -> None:
    """A base-image Docker build cannot hide a Cabal comparator prefetch."""
    task_dir = tmp_path / "task"
    environment_dir = task_dir / "environment"
    problem_dir = task_dir / "evaluation" / "problem"
    library_dir = task_dir / "existing_library" / "measured"
    environment_dir.mkdir(parents=True)
    problem_dir.mkdir(parents=True)
    library_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        """
library_name: example
language: haskell
problems:
  - problem
spine: measured
existing_libraries:
  measured: existing_library/measured
""".lstrip(),
        encoding="utf-8",
    )
    _write_design_task(task_dir)
    (problem_dir / "task.toml").write_text(
        'schema_version = "1.4"\n\n[verifier]\ntimeout_sec = 60.0\n\n'
        "[agent]\ntimeout_sec = 120.0\n\n"
        "[environment]\nbuild_timeout_sec = 120.0\n",
        encoding="utf-8",
    )
    (library_dir / "config.yaml").write_text(
        """
source: measured ==1.0.0
clone: https://example.test/measured.git
ref: v1.0.0
isolation_identifiers: [measured]
""".lstrip(),
        encoding="utf-8",
    )
    (environment_dir / "Dockerfile").write_text(
        "FROM haskell:9.8.4-slim AS ghc\nRUN printf 'measured ==1.0.0\\n' > ldb-existing.cabal\n",
        encoding="utf-8",
    )
    (environment_dir / "environment.cabal").write_text(
        "name: environment\nversion: 0.0.1\nbuild-depends: base\n",
        encoding="utf-8",
    )
    (environment_dir / "cabal.project.freeze").write_text(
        "constraints: base ==4.18.2.1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="preinstalls comparator dependencies"):
        Task.from_dir(task_dir)
