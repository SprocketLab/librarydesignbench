"""Task definition, its existing-library config, and resolved Evaluation Phase problems."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any
from typing import Literal

import yaml
from harbor.models.task.config import TaskConfig
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic import field_validator
from pydantic import model_serializer
from pydantic import model_validator
from pydantic_core import core_schema

from lib_design_bench.models.reports import StaticReference


class ExistingLibraryEntry(BaseModel):
    """A task-local existing-library entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str | None = Field(default=None, min_length=1)
    clone: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    dependent_libraries: tuple[str, ...] = Field(default_factory=tuple)
    related_dependencies: dict[str, str] = Field(default_factory=dict)
    workspace_dependencies: dict[str, str] = Field(default_factory=dict)
    isolation_identifiers: tuple[str, ...]
    config_dir: Path | None = None

    @field_validator("source", "clone", "ref", mode="before")
    @classmethod
    def _validate_source(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "source, clone, and ref must be non-empty strings or null for source"
            )
        return value.strip()

    @field_validator("dependent_libraries", mode="before")
    @classmethod
    def _validate_dependent_libraries(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError(
                "dependent_libraries must be a sequence of dependency names. "
                f"Got {type(value).__name__}: {value!r}"
            )
        dependencies: list[str] = []
        for dependency_name in value:
            if not isinstance(dependency_name, str) or not dependency_name.strip():
                raise ValueError(
                    f"dependent_libraries entries must be non-empty strings: {dependency_name!r}"
                )
            stripped = dependency_name.strip()
            if stripped not in dependencies:
                dependencies.append(stripped)
        return tuple(dependencies)

    @field_validator("related_dependencies", "workspace_dependencies", mode="before")
    @classmethod
    def _validate_dependency_mapping(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(
                "dependency mappings must map dependency names to install specs. "
                f"Got {type(value).__name__}: {value!r}"
            )
        result: dict[str, str] = {}
        for dependency_name, source in value.items():
            if not isinstance(dependency_name, str) or not dependency_name.strip():
                raise ValueError(
                    f"dependency names must be non-empty strings: {dependency_name!r}"
                )
            if not isinstance(source, str) or not source.strip():
                raise ValueError(
                    f"dependency source for {dependency_name!r} must be a non-empty string."
                )
            result[dependency_name.strip()] = source.strip()
        return result

    @field_validator("isolation_identifiers", mode="before")
    @classmethod
    def _validate_isolation_identifiers(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list | tuple) or not value:
            raise ValueError("isolation_identifiers must contain at least one name")
        identifiers: list[str] = []
        for identifier in value:
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError(
                    "isolation_identifiers entries must be non-empty strings"
                )
            normalized = identifier.strip()
            if normalized in identifiers:
                raise ValueError(
                    f"isolation_identifiers contains duplicate {normalized!r}"
                )
            identifiers.append(normalized)
        return tuple(identifiers)

    @model_validator(mode="after")
    def _require_install_dependency(self) -> ExistingLibraryEntry:
        if self.source is None and not self.related_dependencies:
            raise ValueError(
                "source or related_dependencies must install the comparator"
            )
        return self


def load_existing_library_config(path: Path) -> ExistingLibraryEntry:
    """Load one task-local existing-library `config.yaml` file."""
    config_path = path.expanduser().resolve()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return ExistingLibraryEntry.model_validate(data).model_copy(
        update={"config_dir": config_path.parent}
    )


LIBRARY_INSTALL = "/library"
WORKSPACE_LOCATION = "/workspace"
TaskLanguage = Literal["python", "rust", "haskell", "typescript"]


class Task(BaseModel):
    """Validated `task.yaml` content plus its source directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_dir: Path
    library_name: str = Field(min_length=1)
    language: TaskLanguage
    replay_install_cmd: str | None = None
    problems: tuple[str, ...]
    existing_libraries: dict[str, ExistingLibraryEntry] = Field(default_factory=dict)
    spine: str | None = None
    existing_library_problems: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    environment_runtime: str = Field(default="", exclude=True)
    environment_dependencies: tuple[str, ...] = Field(default=(), exclude=True)

    @classmethod
    def _from_path(cls, value: Any) -> Any:
        """Resolve a persisted task directory at the model boundary."""
        if isinstance(value, str | Path):
            task = cls.from_dir(Path(value))
            values = {
                field_name: getattr(task, field_name) for field_name in cls.model_fields
            }
            values["source_dir"] = str(task.source_dir)
            return values
        return value

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_before_validator_function(
            cls._from_path, handler(source)
        )

    @model_serializer(mode="wrap")
    def _to_path(self, handler: Any) -> str:
        """Persist a task by its self-contained source directory."""
        handler(self)
        return str(self.source_dir)

    @field_validator("library_name", "language", mode="before")
    @classmethod
    def _require_str(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(
                f"expected a string, got {type(value).__name__}: {value!r}"
            )
        return value

    @field_validator("replay_install_cmd", mode="before")
    @classmethod
    def _normalize_replay_install_cmd(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                f"replay_install_cmd must be a string or None, got {type(value).__name__}: {value!r}"
            )
        stripped = value.strip()
        return stripped or None

    @field_validator("problems", mode="before")
    @classmethod
    def _validate_problems(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list | tuple) or not value:
            raise ValueError("problems must be a non-empty sequence")
        problems: list[str] = []
        for problem in value:
            if not isinstance(problem, str) or not problem:
                raise ValueError("problems must contain non-empty strings")
            path = Path(problem)
            if (
                path.is_absolute()
                or problem in {".", ".."}
                or len(path.parts) != 1
                or "/" in problem
                or "\\" in problem
            ):
                raise ValueError(
                    f"problem must be a direct-child directory name: {problem!r}"
                )
            problems.append(problem)
        if len(set(problems)) != len(problems):
            raise ValueError("problems must contain unique names")
        return tuple(problems)

    @field_validator("source_dir", mode="before")
    @classmethod
    def _require_source_dir(cls, value: Any) -> str:
        if not isinstance(value, str | Path):
            raise ValueError(
                f"expected a path-like value, got {type(value).__name__}: {value!r}"
            )
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Task source_dir must be a directory: {path}")
        return str(path)

    @field_validator("spine")
    @classmethod
    def _validate_spine(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("spine must be a non-empty existing library name")
        return value

    @field_validator("existing_library_problems", mode="before")
    @classmethod
    def _validate_existing_library_problems(
        cls, value: Any
    ) -> dict[str, tuple[str, ...]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(
                "existing_library_problems must map library names to problem names"
            )
        mappings: dict[str, tuple[str, ...]] = {}
        for library_name, raw_problems in value.items():
            if not isinstance(library_name, str) or not library_name.strip():
                raise ValueError(
                    "existing_library_problems keys must be non-empty strings: "
                    f"{library_name!r}"
                )
            normalized_library = library_name.strip()
            if normalized_library in mappings:
                raise ValueError(
                    "existing_library_problems contains duplicate library name "
                    f"{normalized_library!r}"
                )
            if not isinstance(raw_problems, list | tuple) or not raw_problems:
                raise ValueError(
                    "existing_library_problems values must be non-empty sequences "
                    f"of problem names for {normalized_library!r}"
                )
            problems: list[str] = []
            for problem in raw_problems:
                if not isinstance(problem, str) or not problem.strip():
                    raise ValueError(
                        "existing_library_problems entries must be non-empty strings: "
                        f"{problem!r}"
                    )
                normalized_problem = problem.strip()
                if normalized_problem in problems:
                    raise ValueError(
                        "existing_library_problems contains duplicate problem "
                        f"{normalized_problem!r} for {normalized_library!r}"
                    )
                problems.append(normalized_problem)
            mappings[normalized_library] = tuple(problems)
        return mappings

    def reference_libraries_for_problem(self, problem: str) -> tuple[str, ...]:
        """Return comparator names with a checked-in reference for one problem."""
        if self.existing_library_problems:
            return tuple(
                library_name
                for library_name, problems in self.existing_library_problems.items()
                if problem in problems
            )
        return tuple(self.existing_libraries)

    @model_validator(mode="after")
    def _validate_spine_and_existing_library_problem_metadata(self) -> Task:
        if not self.existing_libraries:
            if self.spine is not None:
                raise ValueError("spine requires at least one existing library")
            return self
        if self.spine is None:
            raise ValueError("Task with existing libraries requires a spine")
        if self.spine not in self.existing_libraries:
            raise ValueError(f"spine {self.spine!r} is not a declared existing library")
        if not self.existing_library_problems:
            return self
        unknown_libraries = tuple(
            library_name
            for library_name in self.existing_library_problems
            if library_name not in self.existing_libraries
        )
        if unknown_libraries:
            unknown_text = ", ".join(repr(name) for name in unknown_libraries)
            raise ValueError(
                "existing_library_problems selects undeclared existing library "
                f"name(s): {unknown_text}"
            )
        selected_by: dict[str, str] = {}
        for library_name, problems in self.existing_library_problems.items():
            for problem in problems:
                if problem not in self.problems:
                    raise ValueError(
                        "existing_library_problems selects inactive problem "
                        f"{problem!r} for {library_name!r}"
                    )
                previous_library = selected_by.get(problem)
                if previous_library is not None:
                    raise ValueError(
                        "existing_library_problems selects problem "
                        f"{problem!r} for both {previous_library!r} and "
                        f"{library_name!r}"
                    )
                selected_by[problem] = library_name
        missing_problems = tuple(
            problem for problem in self.problems if problem not in selected_by
        )
        if missing_problems:
            missing_text = ", ".join(repr(problem) for problem in missing_problems)
            raise ValueError(
                "existing_library_problems must select one existing library for "
                f"every active problem; missing {missing_text}"
            )
        return self

    @classmethod
    def from_dir(cls, source_dir: Path) -> Task:
        """Load and validate a task directory."""
        task_dir = source_dir.expanduser().resolve()
        data = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Task YAML must be a mapping: {task_dir / 'task.yaml'}")
        if existing := data.get("existing_libraries"):
            data["existing_libraries"] = {
                name: load_existing_library_config(
                    _resolve_relative_path(task_dir, path) / "config.yaml"
                )
                for name, path in existing.items()
            }
        data["source_dir"] = task_dir
        task_path = task_dir / "design" / "task.toml"
        if not task_path.is_file():
            raise ValueError(
                f"Design Phase Harbor task must contain task.toml: {task_path}"
            )
        design_task = TaskConfig.model_validate_toml(
            task_path.read_text(encoding="utf-8")
        )
        if design_task.steps:
            raise ValueError(
                "Design Phase Harbor task does not support Harbor steps; it must "
                f"produce one library per author attempt: {task_path}"
            )
        task = cls.model_validate(data)
        runtime, dependencies = _validate_shared_environment(task)
        task = task.model_copy(
            update={
                "environment_runtime": runtime,
                "environment_dependencies": tuple(
                    sorted(dependencies, key=str.casefold)
                ),
            }
        )
        _validate_active_problems(task)
        _validate_environment_dependency_isolation(task)
        return task

    @property
    def name(self) -> str:
        """Return the task directory name."""
        return self.source_dir.name

    @property
    def design_dir(self) -> Path:
        """Return this task's Design Phase Harbor task directory."""
        return self.source_dir / "design"

    @property
    def environment_dir(self) -> Path:
        """Return this task's sole shared Harbor environment directory."""
        return self.source_dir / "environment"

    @property
    def evaluation_dir(self) -> Path:
        """Return this task's Evaluation Phase directory holding each problem's Harbor task."""
        return self.source_dir / "evaluation"

    def problem(self, name: str) -> Problem:
        """Return a resolved declared Evaluation Phase problem."""
        return Problem(task=self, name=name)

    def active_problems(self) -> tuple[Problem, ...]:
        """Return every declared Evaluation Phase problem in declaration order."""
        return tuple(self.problem(name) for name in self.problems)


class Problem(BaseModel):
    """A resolved active Evaluation Phase problem belonging to one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: Task
    name: str

    @model_validator(mode="after")
    def _declared(self) -> Problem:
        if self.name not in self.task.problems:
            raise ValueError(
                f"Problem {self.name!r} is not declared by task {self.task.name!r}"
            )
        return self

    @property
    def dir(self) -> Path:
        """Return this problem's checked-in Harbor task directory."""
        return self.task.evaluation_dir / self.name

    @property
    def harbor_task(self) -> str:
        """Return this problem's Harbor task source suffix."""
        return f"evaluation/{self.name}"

    def existing_libraries(self) -> tuple[str, ...]:
        """Return comparator names with checked-in references for this problem."""
        return self.task.reference_libraries_for_problem(self.name)

    def solution_dir(self, arm_name: str) -> Path:
        """Return a condition-specific checked-in solution directory."""
        return self.dir / "solution" / arm_name

    def static_reference(self) -> StaticReference | None:
        """Load this problem's checked-in static reference when available."""
        reference_path = self.dir / "tests" / "static_reference.json"
        if not reference_path.is_file():
            return None
        try:
            reference = StaticReference.from_json(reference_path)
        except ValueError as error:
            raise ValueError(
                f"Invalid static reference: {reference_path}: {error}"
            ) from error
        declared_arms = {"no-library", *self.task.existing_libraries}
        if reference.library not in declared_arms:
            raise ValueError(
                "Static reference library is not a declared arm for "
                f"{self.task.name!r}: {reference.library!r}"
            )
        source = self.solution_dir(reference.library)
        if not source.is_dir():
            raise ValueError(
                f"Static reference library has no problem solution directory: {source}"
            )
        return reference

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Problem):
            return NotImplemented
        return self.task.source_dir == other.task.source_dir and self.name == other.name


def _validate_shared_environment(task: Task) -> tuple[str, set[str]]:
    environment_dir = task.environment_dir
    if not environment_dir.is_dir():
        raise ValueError(f"Task requires a shared environment: {environment_dir}")
    dockerfile = environment_dir / "Dockerfile"
    if not dockerfile.is_file():
        raise ValueError(f"Shared environment requires Dockerfile: {dockerfile}")
    setup = environment_dir / "setup.sh"
    if setup.exists():
        raise ValueError(f"Shared environment cannot define a setup hook: {setup}")
    if task.language == "python":
        requirements = environment_dir / "requirements.txt"
        if not requirements.is_file():
            raise ValueError(
                f"Python shared environment requires requirements.txt: {requirements}"
            )
        dependencies: set[str] = set()
        for raw_line in requirements.read_text(encoding="utf-8").splitlines():
            requirement = raw_line.partition("#")[0].strip()
            if requirement and (requirement.startswith("-") or "==" not in requirement):
                raise ValueError(
                    "Python environment dependencies must use exact versions for "
                    f"reproducible builds: {requirement!r} in {requirements}"
                )
            if match := re.match(r"([A-Za-z0-9_.-]+)", requirement):
                dependencies.add(match.group(1))
        return "python (offline)", dependencies
    elif task.language == "rust":
        return "rust (cargo offline)", _validate_rust_environment(environment_dir)
    elif task.language == "typescript":
        return "typescript (npm offline)", _validate_typescript_environment(
            environment_dir
        )
    manifests = tuple(environment_dir.glob("*.cabal"))
    if len(manifests) != 1:
        raise ValueError(
            "Haskell shared environment requires exactly one Cabal manifest: "
            f"{environment_dir}"
        )
    freeze = environment_dir / "cabal.project.freeze"
    if not freeze.is_file():
        raise ValueError(
            f"Haskell shared environment requires cabal.project.freeze: {freeze}"
        )
    return "haskell (cabal offline)", _cabal_dependency_names(
        environment_dir
    ) | _ghc_boot_packages(environment_dir / "Dockerfile")


def _validate_typescript_environment(environment_dir: Path) -> set[str]:
    package_path = environment_dir / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError(
            f"TypeScript shared environment requires package.json: {package_path}"
        ) from error
    dependencies = package.get("dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        raise ValueError(
            f"TypeScript shared environment requires dependencies: {package_path}"
        )
    for name, version in dependencies.items():
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version)
        ):
            raise ValueError(
                "TypeScript environment dependencies must use exact versions: "
                f"{name!r}: {version!r} in {package_path}"
            )
    return {str(name) for name in dependencies}


def _validate_rust_environment(environment_dir: Path) -> set[str]:
    manifest_path = environment_dir / "Cargo.toml"
    lock_path = environment_dir / "Cargo.lock"
    if not manifest_path.is_file():
        raise ValueError(
            f"Rust shared environment requires Cargo.toml: {manifest_path}"
        )
    if not lock_path.is_file():
        raise ValueError(f"Rust shared environment requires Cargo.lock: {lock_path}")
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    dependencies = manifest.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ValueError(f"Rust environment has invalid dependencies: {manifest_path}")
    for name, spec in dependencies.items():
        if not isinstance(name, str):
            raise ValueError(
                f"Rust environment has invalid dependency name: {manifest_path}"
            )
        version = (
            spec
            if isinstance(spec, str)
            else spec.get("version")
            if isinstance(spec, dict)
            else None
        )
        if version is None:
            continue
        if not isinstance(version, str) or not version.startswith("="):
            raise ValueError(
                f"Rust environment dependency {name!r} must use an exact version: "
                f"{manifest_path}"
            )
    return {
        str(spec["package"])
        if isinstance(spec, dict) and "package" in spec
        else str(name)
        for name, spec in dependencies.items()
    }


def _validate_active_problems(task: Task) -> None:
    """Require the filesystem entries Harbor needs to parse each problem's Harbor task."""
    for problem_name in task.problems:
        problem_dir = task.problem(problem_name).dir
        if not problem_dir.is_dir():
            raise ValueError(f"Active problem directory does not exist: {problem_dir}")
        task_path = problem_dir / "task.toml"
        if not task_path.is_file():
            raise ValueError(f"Active problem must contain task.toml: {task_path}")
        if task.language == "rust":
            _validate_rust_workspace_starter(problem_dir / "workspace")


def _validate_rust_workspace_starter(workspace_dir: Path) -> None:
    """Require each active Rust problem to provide a runnable application starter."""
    manifest_path = workspace_dir / "Cargo.toml"
    main_path = workspace_dir / "src" / "main.rs"
    if not manifest_path.is_file():
        raise ValueError(
            f"Active Rust problem must contain Cargo.toml: {manifest_path}"
        )
    if not main_path.is_file():
        raise ValueError(f"Active Rust problem must contain src/main.rs: {main_path}")
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(
            f"Active Rust problem has invalid Cargo.toml: {manifest_path}"
        ) from error
    package = manifest.get("package")
    if not isinstance(package, dict) or any(
        not isinstance(package.get(field), str) or not package[field].strip()
        for field in ("name", "version", "edition")
    ):
        raise ValueError(
            "Active Rust problem Cargo.toml requires a package name, version, and "
            f"edition: {manifest_path}"
        )
    bins = manifest.get("bin", [])
    has_explicit_main = isinstance(bins, list) and any(
        isinstance(binary, dict) and binary.get("path") == "src/main.rs"
        for binary in bins
    )
    if package.get("autobins") is False and not has_explicit_main:
        raise ValueError(
            "Active Rust problem Cargo.toml must expose src/main.rs as an executable "
            f"target: {manifest_path}"
        )


def _validate_environment_dependency_isolation(task: Task) -> None:
    """Keep production comparator packages out of the shared base environment."""
    protected = {
        _normalize_dependency_name(name) for name in _comparator_dependency_names(task)
    }
    preinstalled = {
        _normalize_dependency_name(name)
        for name in (
            *task.environment_dependencies,
            *_docker_dependency_names(task.environment_dir / "Dockerfile"),
        )
    }
    leaked = sorted(protected.intersection(preinstalled))
    if leaked:
        raise ValueError(
            "Shared base environment preinstalls comparator dependencies for "
            f"{task.name!r}: {', '.join(leaked)}. Comparator dependencies must "
            "be installed only in the existing-library image."
        )


def _comparator_dependency_names(task: Task) -> set[str]:
    """Return declared package names that belong only to comparator images."""
    names = set(task.existing_libraries)
    for library in task.existing_libraries.values():
        names.update(library.dependent_libraries)
        names.update(library.related_dependencies)
        source_match = (
            re.match(r"\s*([A-Za-z0-9_.-]+)(?:\s*==|@)", library.source)
            if library.source is not None
            else None
        )
        if source_match is not None:
            names.add(source_match.group(1))
    return names


# Library packages GHC installs in its global package database, so every Haskell
# environment can depend on them offline without declaring them in Cabal files.
# parsec also ships with GHC but is a parsing comparator, so it is never advertised.
_GHC_BOOT_LIBRARIES: dict[str, frozenset[str]] = {
    "9.8": frozenset(
        {
            "array",
            "base",
            "binary",
            "bytestring",
            "containers",
            "deepseq",
            "directory",
            "exceptions",
            "filepath",
            "mtl",
            "pretty",
            "process",
            "stm",
            "template-haskell",
            "text",
            "time",
            "transformers",
            "unix",
        }
    ),
}


def _ghc_boot_packages(dockerfile: Path) -> set[str]:
    """Return the boot libraries shipped by the GHC stage the Dockerfile copies."""
    stages = re.findall(
        r"^FROM\s+haskell:(\d+\.\d+)\S*\s+AS\s+ghc\b",
        dockerfile.read_text(encoding="utf-8"),
        flags=re.M | re.I,
    )
    if len(stages) != 1:
        raise ValueError(
            "Haskell shared environment must build GHC from exactly one "
            f"`FROM haskell:<version> AS ghc` stage: {dockerfile}"
        )
    series = stages[0]
    if series not in _GHC_BOOT_LIBRARIES:
        raise ValueError(
            f"No GHC boot library list for haskell:{series} images; add one to "
            f"_GHC_BOOT_LIBRARIES: {dockerfile}"
        )
    return set(_GHC_BOOT_LIBRARIES[series])


def _cabal_dependency_names(environment_dir: Path) -> set[str]:
    """Return direct build dependencies from Cabal environment manifests."""
    names: set[str] = set()
    for path in environment_dir.glob("*.cabal"):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(
            r"(?ms)^\s*build-depends:\s*(.*?)(?=^\s*[A-Za-z-]+:|^\S|\Z)", text
        ):
            names.update(re.findall(r"([A-Za-z][A-Za-z0-9-]*)\s*(?:[<>=]|,|$)", block))
    return names


def _docker_dependency_names(path: Path) -> set[str]:
    """Return package names installed directly by base-image package commands."""
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8").replace("\\\n", " ").replace(r"\n", "\n")
    names: set[str] = set()
    for command in re.findall(
        r"(?:(?:python\d*\s+-m\s+)?pip\d*|npm)\s+install\s+([^;&\n]+)",
        text,
    ):
        for token in shlex.split(command):
            if token.startswith("-"):
                continue
            name = re.split(r"(?:==|>=|<=|~=|@)", token, maxsplit=1)[0]
            if re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                names.add(name)
    for package in re.findall(r"(?<![A-Za-z0-9-])([A-Za-z][A-Za-z0-9-]*)\s*==", text):
        names.add(package)
    return names


def _normalize_dependency_name(name: str) -> str:
    """Compare package names using Python/Rust/npm separator equivalence."""
    return re.sub(r"[-_.]+", "-", name).casefold()


def _resolve_relative_path(task_dir: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return task_dir / path
