"""Language policies used by condition-aware library materialization."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Protocol

from harbor.models.task.config import HealthcheckConfig

from lib_design_bench.models import LIBRARY_INSTALL
from lib_design_bench.models import WORKSPACE_LOCATION
from lib_design_bench.models.task import ExistingLibraryEntry
from lib_design_bench.models.task import TaskLanguage


@dataclass(frozen=True)
class _AuthoredLibraryPlan:
    """Language-owned setup for an authored library artifact."""

    setup_script: str
    preflight_command: str


@dataclass(frozen=True)
class ExistingImagePlan:
    """Language-owned inputs for an existing-library image."""

    dockerfile_steps: str
    setup_script: str
    readiness_command: str
    files: tuple[tuple[str, str], ...] = ()


class _LanguageMaterializationPolicy(Protocol):
    """Own language variation behind the materializer's internal seam."""

    def authored_plan(
        self,
        *,
        design: bool = False,
    ) -> _AuthoredLibraryPlan: ...

    def existing_plan(
        self,
        existing: ExistingLibraryEntry,
        environment_packages: tuple[str, ...],
    ) -> ExistingImagePlan:
        """Plan the comparator image for one condition.

        `environment_packages` names the shared environment's own packages.
        Languages whose comparator install is independent of them ignore it;
        Haskell resolves both sets in one solve so the image holds a single
        version of every package it preinstalls.
        """
        ...

    def blacklist_healthcheck(
        self, identifiers: tuple[str, ...]
    ) -> HealthcheckConfig: ...


@dataclass(frozen=True)
class _PythonMaterializationPolicy:
    """Materialize Python packages into the shared workspace virtualenv."""

    library_name: str

    def authored_plan(
        self,
        *,
        design: bool = False,
    ) -> _AuthoredLibraryPlan:
        library_path = WORKSPACE_LOCATION if design else LIBRARY_INSTALL
        workspace_path = "/tmp/ldb-library-readiness" if design else WORKSPACE_LOCATION
        virtualenv_setup = (
            'if [ ! -x "$python_bin" ]; then python -m venv "$workspace_path/.venv"; fi'
        )
        if design:
            virtualenv_setup = """
mkdir -p "$workspace_path"
python -m venv "$workspace_path/.venv"
uv pip install --python "$python_bin" --offline \\
  --requirements /tests/ldb-environment-requirements.txt
"""
        setup_script = rf"""#!/usr/bin/env bash
set -euo pipefail
library_path={shlex.quote(library_path)}
workspace_path={shlex.quote(workspace_path)}
python_bin="$workspace_path/.venv/bin/python"
{virtualenv_setup}
install_path="$library_path"
if [ "$library_path" != "$workspace_path" ]; then
  rm -rf /workspaces/ldb-library
  mkdir -p /workspaces
  cp -a "$library_path" /workspaces/ldb-library
  install_path=/workspaces/ldb-library
fi
uv pip install --python "$python_bin" --offline --no-build-isolation "$install_path"
# -P keeps the working directory off sys.path: an author's leftover
# <name>.egg-info there would otherwise count as a second distribution.
"$python_bin" -P - {shlex.quote(self.library_name)} <<'PY'
import importlib
import importlib.metadata
import re
import sys


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


expected = normalize(sys.argv[1])
matches = [
    distribution
    for distribution in importlib.metadata.distributions()
    if (name := distribution.metadata.get("Name")) is not None
    and normalize(name) == expected
]
if len(matches) != 1:
    raise SystemExit(f"expected one installed distribution named {{sys.argv[1]!r}}, found {{len(matches)}}")
distribution = matches[0]
top_level_text = distribution.read_text("top_level.txt") or ""
top_levels = {{name.strip() for name in top_level_text.splitlines() if name.strip()}}
if not top_levels:
    for file in distribution.files or ():
        first = file.parts[0]
        if first.endswith((".dist-info", ".data")) or first == "__pycache__":
            continue
        if len(file.parts) == 1 and first.endswith(".py"):
            top_levels.add(first.removesuffix(".py"))
        elif len(file.parts) > 1:
            top_levels.add(first)
for module in sorted(top_levels):
    if module.isidentifier():
        importlib.import_module(module)
PY
"""
        return _AuthoredLibraryPlan(
            setup_script=setup_script,
            preflight_command="command -v python >/dev/null && command -v uv >/dev/null",
        )

    def existing_plan(
        self,
        existing: ExistingLibraryEntry,
        environment_packages: tuple[str, ...],
    ) -> ExistingImagePlan:
        targets = tuple(existing.related_dependencies.values())
        if existing.source is not None:
            targets = (existing.source, *targets)
        quoted_targets = " ".join(shlex.quote(target) for target in targets)
        imports = "; ".join(
            f"import {name.replace('-', '_')}"
            for name in existing.isolation_identifiers
        )
        return ExistingImagePlan(
            dockerfile_steps=(
                "RUN PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX= UV_OFFLINE=false "
                "uv pip install --python /workspace/.venv/bin/python "
                f"--force-reinstall {quoted_targets}\n"
            ),
            setup_script=r"""#!/usr/bin/env bash
set -euo pipefail
workspace_path="$1"
library_path="$2"
test -d "$library_path"
python_bin="$workspace_path/.venv/bin/python"
if [ ! -x "$python_bin" ]; then
  mkdir -p "$workspace_path"
  python -m venv "$workspace_path/.venv"
fi
""",
            readiness_command=(
                f"/workspace/.venv/bin/python -c {shlex.quote(imports)}"
            ),
        )

    def blacklist_healthcheck(self, identifiers: tuple[str, ...]) -> HealthcheckConfig:
        command = "/workspace/.venv/bin/python -c " + shlex.quote(
            "import importlib.util,sys; "
            f"names={list(identifiers)!r}; "
            "found=[name for name in names if importlib.util.find_spec(name) is not None]; "
            "print('comparator leakage: '+', '.join(found), file=sys.stderr) if found else None; "
            "sys.exit(bool(found))"
        )
        return HealthcheckConfig(command=command, retries=1)


def _rust_workspace_setup(
    direct_dependencies: dict[str, str],
    path_dependency: str | None = None,
    *,
    offline: bool = False,
) -> str:
    """Return setup that updates a Rust application manifest without duplicates.

    `offline` makes the tomlkit step read the image's uv cache without touching
    the package index; Design Phase readiness runs with no network at all.
    """
    direct_dependencies_json = shlex.quote(
        json.dumps(
            {
                name: _rust_dependency_value(source)
                for name, source in direct_dependencies.items()
            }
        )
    )
    path_dependency_name = shlex.quote(path_dependency or "")
    return rf"""mkdir -p "$workspace_path"
if [ ! -f "$workspace_path/Cargo.toml" ]; then
  cat > "$workspace_path/Cargo.toml" <<'EOF'
[package]
name = "ldb-workspace-app"
version = "0.1.0"
edition = "2021"
publish = false
EOF
fi
mkdir -p "$workspace_path/src"
if [ ! -f "$workspace_path/src/main.rs" ]; then
  printf 'fn main() {{}}\n' > "$workspace_path/src/main.rs"
fi
RUST_DIRECT_DEPENDENCIES={direct_dependencies_json} \
RUST_PATH_DEPENDENCY={path_dependency_name} \
RUST_LIBRARY_PATH="$library_path" \
uv run --isolated --no-project{" --offline" if offline else ""} --with tomlkit==0.13.3 \
  python - "$workspace_path/Cargo.toml" <<'PY'
import json
import os
import sys
import tomllib
from pathlib import Path

import tomlkit

manifest_path = Path(sys.argv[1])
library_path = Path(os.environ["RUST_LIBRARY_PATH"])
direct_dependencies = json.loads(os.environ["RUST_DIRECT_DEPENDENCIES"])
path_dependency = os.environ["RUST_PATH_DEPENDENCY"]

if path_dependency:
    direct_dependencies[path_dependency] = (
        "{{ path = " + json.dumps(str(library_path)) + " }}"
    )
manifest = tomlkit.parse(manifest_path.read_text(encoding="utf-8"))
if direct_dependencies:
    dependencies = manifest.setdefault("dependencies", tomlkit.table())
    for name, source in direct_dependencies.items():
        dependencies[name] = tomlkit.parse("dependency = " + source)["dependency"]
patches = {{}}
for candidate in sorted(library_path.rglob("Cargo.toml")):
    if len(candidate.relative_to(library_path).parts) > 3:
        continue
    try:
        package = tomllib.loads(candidate.read_text(encoding="utf-8")).get("package")
    except tomllib.TOMLDecodeError:
        continue
    if isinstance(package, dict) and isinstance(package.get("name"), str):
        patches.setdefault(package["name"], str(candidate.parent))
if patches:
    registry_patches = manifest.setdefault("patch", tomlkit.table()).setdefault(
        "crates-io", tomlkit.table()
    )
    for name, path in patches.items():
        entry = tomlkit.inline_table()
        entry["path"] = path
        registry_patches[name] = entry
manifest_path.write_text(tomlkit.dumps(manifest), encoding="utf-8")
PY
"""


@dataclass(frozen=True)
class _RustMaterializationPolicy:
    """Materialize Cargo packages and their selected dependency closure."""

    library_name: str

    def authored_plan(
        self,
        *,
        design: bool = False,
    ) -> _AuthoredLibraryPlan:
        library_path = WORKSPACE_LOCATION if design else LIBRARY_INSTALL
        workspace_path = "/tmp/ldb-library-readiness" if design else WORKSPACE_LOCATION
        setup_script = rf"""#!/usr/bin/env bash
set -euo pipefail
library_path={shlex.quote(library_path)}
workspace_path={shlex.quote(workspace_path)}
test -f "$library_path/Cargo.toml"
test -f "$library_path/Cargo.lock"
package_name="$(sed -n '/^\[package\]/,/^\[/s/^name[[:space:]]*=[[:space:]]*["'\'']\([^"'\'']*\)["'\''].*/\1/p' "$library_path/Cargo.toml" | head -n1)"
test "$package_name" = {shlex.quote(self.library_name)}
{_rust_workspace_setup({}, self.library_name, offline=design)}
cargo build --locked --offline --lib --manifest-path "$library_path/Cargo.toml" \
  --target-dir "$workspace_path/target" >/dev/null
"""
        return _AuthoredLibraryPlan(
            setup_script=setup_script,
            preflight_command="command -v cargo >/dev/null",
        )

    def existing_plan(
        self,
        existing: ExistingLibraryEntry,
        environment_packages: tuple[str, ...],
    ) -> ExistingImagePlan:
        dependencies: dict[str, str] = dict(existing.related_dependencies)
        if existing.source is not None:
            dependencies[self.library_name] = existing.source
        manifest = _rust_closure_manifest(dependencies)
        setup_script = f"""#!/usr/bin/env bash
set -euo pipefail
workspace_path="$1"
library_path="$2"
test -f "$library_path/Cargo.toml"
{_rust_workspace_setup(existing.workspace_dependencies)}
"""
        return ExistingImagePlan(
            files=(
                ("ldb-existing-Cargo.toml", manifest),
                (
                    "ldb-workspace-Cargo.toml",
                    _rust_closure_manifest(existing.workspace_dependencies),
                ),
            ),
            dockerfile_steps=(
                "COPY ldb-existing-Cargo.toml /tmp/ldb-existing/Cargo.toml\n"
                "COPY ldb-workspace-Cargo.toml /tmp/ldb-workspace/Cargo.toml\n"
                "RUN mkdir -p /tmp/ldb-existing/src "
                "&& printf 'pub fn placeholder() {}\\n' > /tmp/ldb-existing/src/lib.rs "
                "&& CARGO_NET_OFFLINE=false cargo fetch "
                "--manifest-path /tmp/ldb-existing/Cargo.toml "
                f"&& CARGO_NET_OFFLINE=false cargo fetch --manifest-path {LIBRARY_INSTALL}/Cargo.toml "
                "&& mkdir -p /tmp/ldb-workspace/src "
                "&& touch /tmp/ldb-workspace/src/lib.rs "
                "&& CARGO_NET_OFFLINE=false cargo fetch --manifest-path /tmp/ldb-workspace/Cargo.toml "
                "&& rm -rf /tmp/ldb-existing /tmp/ldb-workspace\n"
            ),
            setup_script=setup_script,
            readiness_command=f"test -f {LIBRARY_INSTALL}/Cargo.toml",
        )

    def blacklist_healthcheck(self, identifiers: tuple[str, ...]) -> HealthcheckConfig:
        return _shell_blacklist_healthcheck(
            identifiers,
            'grep -Fqx "name = \\"$identifier\\"" /opt/ldb-environment/Cargo.lock',
        )


@dataclass(frozen=True)
class _TypeScriptMaterializationPolicy:
    """Materialize npm packages beside the neutral offline dependencies."""

    library_name: str

    def authored_plan(
        self,
        *,
        design: bool = False,
    ) -> _AuthoredLibraryPlan:
        library_path = WORKSPACE_LOCATION if design else LIBRARY_INSTALL
        workspace_path = "/tmp/ldb-library-readiness" if design else WORKSPACE_LOCATION
        return _AuthoredLibraryPlan(
            setup_script=_typescript_library_setup(
                library_path,
                self.library_name,
                workspace_path,
            ),
            preflight_command="command -v node >/dev/null && command -v tsc >/dev/null",
        )

    def existing_plan(
        self,
        existing: ExistingLibraryEntry,
        environment_packages: tuple[str, ...],
    ) -> ExistingImagePlan:
        targets = tuple(
            _npm_install_target(name, source)
            for name, source in existing.related_dependencies.items()
        )
        dockerfile_steps = ""
        if targets:
            dockerfile_steps = (
                "RUN cd /library && npm install --no-audit --no-fund "
                "--ignore-scripts "
                + " ".join(shlex.quote(target) for target in targets)
                + "\n"
            )
        return ExistingImagePlan(
            dockerfile_steps=dockerfile_steps,
            setup_script=_typescript_library_setup(
                LIBRARY_INSTALL,
                self.library_name,
                WORKSPACE_LOCATION,
            ),
            readiness_command=(
                "node -e "
                + shlex.quote(
                    "const pkg=require('/library/package.json'); "
                    f"if (pkg.name !== {self.library_name!r}) process.exit(1)"
                )
            ),
        )

    def blacklist_healthcheck(self, identifiers: tuple[str, ...]) -> HealthcheckConfig:
        return _shell_blacklist_healthcheck(
            identifiers,
            'test -e "/opt/neutral/node_modules/$identifier"',
        )


@dataclass(frozen=True)
class _HaskellMaterializationPolicy:
    """Materialize Cabal packages into the shared Cabal store."""

    library_name: str

    def authored_plan(
        self,
        *,
        design: bool = False,
    ) -> _AuthoredLibraryPlan:
        library_path = WORKSPACE_LOCATION if design else LIBRARY_INSTALL
        # The library is always proven to build alone from a throwaway project;
        # the application workspace has no .cabal until the agent writes one,
        # so the Evaluation Phase only leaves it a project file that reaches the
        # library.
        workspace_project = (
            ""
            if design
            else "printf 'packages: . %s\\n' \"$library_path\" > /workspace/cabal.project\n"
        )
        setup_script = f"""#!/usr/bin/env bash
set -euo pipefail
library_path={shlex.quote(library_path)}
readiness_path=/tmp/ldb-library-readiness
test -f "$library_path/cabal.project.freeze"
manifest="$(find "$library_path" -maxdepth 1 -name '*.cabal' -type f -print -quit)"
test -n "$manifest"
test "$(sed -n 's/^[[:space:]]*[Nn][Aa][Mm][Ee]:[[:space:]]*//p' "$manifest" | head -n1)" = {shlex.quote(self.library_name)}
mkdir -p "$readiness_path"
printf 'packages: %s\\n' "$library_path" > "$readiness_path/cabal.project"
cabal build all --offline --project-dir="$readiness_path"
{workspace_project}"""
        return _AuthoredLibraryPlan(
            setup_script=setup_script,
            preflight_command=(
                "command -v cabal >/dev/null && command -v ghc-pkg >/dev/null"
            ),
        )

    def existing_plan(
        self,
        existing: ExistingLibraryEntry,
        environment_packages: tuple[str, ...],
    ) -> ExistingImagePlan:
        manifest = _haskell_closure_manifest(
            self.library_name,
            existing.source,
            existing.dependent_libraries,
            existing.related_dependencies,
            environment_packages,
        )
        # The workspace freeze only pins projects under /workspace, so a scratch
        # project an agent builds elsewhere resolves whatever Hackage advertises
        # and then fails offline. Global constraints pin every project instead.
        # The base image's constraint lines are dropped first, so this union freeze
        # stays the single constraint source. The solve reuses the Hackage index the
        # shared environment pinned; a fresh `cabal update` would resolve whatever
        # Hackage serves at build time, and a verifier image rebuilt later would
        # reject the freeze the agent shipped.
        freeze_to_config = _haskell_freeze_to_config(
            "cabal.project.freeze", '"$CABAL_DIR/config"'
        )
        return ExistingImagePlan(
            files=(("ldb-existing.cabal", manifest),),
            dockerfile_steps=(
                "COPY ldb-existing.cabal /tmp/ldb-existing/ldb-existing.cabal\n"
                "RUN mkdir -p /tmp/ldb-existing/src "
                "&& printf 'module LDBExisting where\\n' > /tmp/ldb-existing/src/LDBExisting.hs "
                "&& sed -i '/^constraint: /d' \"$CABAL_DIR/config\" "
                "&& cd /tmp/ldb-existing "
                "&& cabal build --only-dependencies --disable-tests --disable-benchmarks "
                "&& cabal freeze --disable-tests --disable-benchmarks "
                "&& install -Dm644 cabal.project.freeze /opt/ldb-existing/cabal.project.freeze "
                f"&& {freeze_to_config} "
                "&& rm -rf /tmp/ldb-existing /root/.cabal/logs\n"
            ),
            setup_script="""#!/usr/bin/env bash
set -euo pipefail
workspace_path="$1"
library_path="$2"
test -d "$library_path"
test -f /opt/ldb-existing/cabal.project.freeze
mkdir -p "$workspace_path"
printf 'packages: .\n' > "$workspace_path/cabal.project"
cp /opt/ldb-existing/cabal.project.freeze "$workspace_path/cabal.project.freeze"
""",
            readiness_command=(
                f'test -n "$(find {LIBRARY_INSTALL} '
                "-name '*.cabal' -type f -print -quit)\""
            ),
        )

    def blacklist_healthcheck(self, identifiers: tuple[str, ...]) -> HealthcheckConfig:
        return _shell_blacklist_healthcheck(
            identifiers, 'ghc-pkg latest "$identifier" >/dev/null 2>&1'
        )


def language_materialization_policy(
    language: TaskLanguage,
    library_name: str,
) -> _LanguageMaterializationPolicy:
    """Return the policy for one validated task language."""
    if language == "python":
        return _PythonMaterializationPolicy(library_name)
    if language == "rust":
        return _RustMaterializationPolicy(library_name)
    if language == "typescript":
        return _TypeScriptMaterializationPolicy(library_name)
    return _HaskellMaterializationPolicy(library_name)


def _typescript_library_setup(
    library_path: str, library_name: str, workspace_path: str
) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail
workspace_path={shlex.quote(workspace_path)}
neutral_dir=/opt/neutral/node_modules
library_path={shlex.quote(library_path)}
test -f "$library_path/package.json"
mkdir -p "$workspace_path/node_modules"
for modules_dir in "$neutral_dir" "$library_path/node_modules"; do
  [[ -d "$modules_dir" ]] || continue
  for package_path in "$modules_dir"/*; do
    package_name="$(basename "$package_path")"
    if [[ "$package_name" == @* ]]; then
      mkdir -p "$workspace_path/node_modules/$package_name"
      for scoped_package in "$package_path"/*; do
        target="$workspace_path/node_modules/$package_name/$(basename "$scoped_package")"
        rm -rf "$target"
        ln -s "$scoped_package" "$target"
      done
    else
      target="$workspace_path/node_modules/$package_name"
      rm -rf "$target"
      ln -s "$package_path" "$target"
    fi
  done
done
package_name="$(node -e 'const packageJson=require(process.argv[1]); process.stdout.write(packageJson.name || "")' "$library_path/package.json")"
test "$package_name" = {shlex.quote(library_name)}
if [[ "$package_name" == @*/* ]]; then
  mkdir -p "$workspace_path/node_modules/${{package_name%%/*}}"
fi
ln -sfn "$library_path" "$workspace_path/node_modules/$package_name"
"""


def _npm_install_target(name: str, source: str) -> str:
    """Return one npm install argument from a declared related dependency."""
    stripped = source.strip()
    return stripped if stripped.startswith(f"{name}@") else f"{name}@{stripped}"


def _shell_blacklist_healthcheck(
    identifiers: tuple[str, ...], probe: str
) -> HealthcheckConfig:
    names = " ".join(shlex.quote(name) for name in identifiers)
    command = "bash -c " + shlex.quote(
        f"for identifier in {names}; do "
        f"if {probe}; then "
        'echo "comparator leakage: $identifier" >&2; exit 1; fi; '
        "done"
    )
    return HealthcheckConfig(command=command, retries=1)


def _rust_closure_manifest(dependencies: dict[str, str]) -> str:
    dependency_lines = "\n".join(
        f"{_toml_key(name)} = {_rust_dependency_value(source)}"
        for name, source in dependencies.items()
    )
    return (
        "[package]\n"
        'name = "ldb-existing-closure"\n'
        'version = "0.0.0"\n'
        'edition = "2021"\n'
        "publish = false\n\n"
        "[lib]\n"
        'path = "src/lib.rs"\n\n'
        "[dependencies]\n"
        f"{dependency_lines}\n"
    )


def _rust_dependency_value(source: str) -> str:
    stripped = source.strip()
    if stripped.startswith(("{", '"')):
        return stripped
    if stripped.startswith(("http://", "https://", "ssh://", "git@")):
        return f"{{ git = {json.dumps(stripped)} }}"
    return json.dumps(stripped)


def _toml_key(value: str) -> str:
    if value.replace("-", "_").replace("_", "").isalnum():
        return value
    return json.dumps(value)


def _haskell_closure_manifest(
    library_name: str,
    source: str | None,
    dependent_libraries: tuple[str, ...],
    related_dependencies: dict[str, str],
    environment_packages: tuple[str, ...],
) -> str:
    """Build the throwaway package whose solve defines the image's closure.

    The environment's own packages join the comparator's, so one solve pins one
    version of each. A comparator-only freeze leaves the environment's packages
    unpinned in `/workspace` and in the image's global constraints, and an
    application naming both sets then resolves versions the offline image never
    stored.
    """
    names = dict.fromkeys(
        (
            library_name,
            *dependent_libraries,
            *related_dependencies.keys(),
            *(name for name in environment_packages if name != "base"),
        )
    )
    dependencies = ["base >=4.14 && <5"]
    for name in names:
        constraint = source if name == library_name else related_dependencies.get(name)
        dependencies.append(
            _haskell_dependency_constraint(name, constraint) if constraint else name
        )
    dependency_lines = "\n".join(
        f"      {'' if index == 0 else ', '}{dependency}"
        for index, dependency in enumerate(dependencies)
    )
    return (
        "cabal-version: 3.0\n"
        "name: ldb-existing\n"
        "version: 0.0.0.0\n"
        "build-type: Simple\n\n"
        "library\n"
        "  exposed-modules: LDBExisting\n"
        "  hs-source-dirs: src\n"
        "  build-depends:\n"
        f"{dependency_lines}\n"
        "  default-language: Haskell2010\n"
    )


def _haskell_freeze_to_config(freeze_path: str, config_path: str) -> str:
    """Build the command appending a freeze file's entries as global constraints.

    A `cabal.project.freeze` only pins the project that contains it, so a scratch
    project built outside /workspace resolves newly indexed Hackage versions the
    offline image never stored. Singular `constraint:` lines in the Cabal config
    pin every project in the sandbox to the installed closure instead.
    """
    program = (
        '/^[^[:space:]]/ { entry = ($0 ~ /^constraints:/); sub(/^constraints:/, "") } '
        "entry { "
        'sub(/^[[:space:]]+/, ""); sub(/[[:space:]]*,?[[:space:]]*$/, ""); '
        'if ($0 != "") print "constraint: " $0 '
        "}"
    )
    return f"awk '{program}' {freeze_path} >> {config_path}"


def _haskell_dependency_constraint(library_name: str, source: str) -> str:
    stripped = source.strip()
    first_token = stripped.split(maxsplit=1)[0]
    if first_token == library_name:
        return stripped
    if stripped.startswith(("==", ">=", "<=", ">", "<", "^>=", "&&")):
        return f"{library_name} {stripped}"
    return f"{library_name} =={stripped}"
