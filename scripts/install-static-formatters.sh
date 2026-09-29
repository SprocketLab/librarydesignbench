#!/usr/bin/env bash
# Install the exact external normalizers required by static analysis.
set -euo pipefail

if (( $# > 1 )); then
    printf 'Usage: %s [install-root]\n' "$0" >&2
    exit 2
fi

ROOT="${1:-${STATIC_FORMATTERS_HOME:-$HOME/.local/share/lib-design-bench/static-formatters}}"
BIN="$ROOT/bin"
NODE="$ROOT/node"
RUST_TOOLCHAIN="1.98.1"

for command in uv npm rustup cabal; do
    command -v "$command" >/dev/null || {
        printf 'Required installer command is unavailable: %s\n' "$command" >&2
        exit 1
    }
done

mkdir -p "$BIN"
UV_TOOL_DIR="$ROOT/ruff-tools" UV_TOOL_BIN_DIR="$BIN" \
    uv tool install --force "ruff==0.16.6"

npm install --prefix "$NODE" "prettier@3.9.6"
ln -sf "$NODE/node_modules/.bin/prettier" "$BIN/prettier"

rustup toolchain install "$RUST_TOOLCHAIN" --profile minimal --component rustfmt
ln -sf "$(rustup which --toolchain "$RUST_TOOLCHAIN" rustfmt)" "$BIN/rustfmt"

cabal install fourmolu-0.20.1.0 --installdir="$BIN" --overwrite-policy=always
printf 'Installed static normalizers in %s. Add it to PATH.\n' "$BIN"
