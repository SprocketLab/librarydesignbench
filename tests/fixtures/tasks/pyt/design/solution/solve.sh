#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace
rm -rf /workspace/*
SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cp -a "$SOLUTION_DIR/README.md" /workspace/README.md
cp -a "$SOLUTION_DIR/pyproject.toml" /workspace/pyproject.toml
cp -a "$SOLUTION_DIR/uv.lock" /workspace/uv.lock
cp -a "$SOLUTION_DIR/src" /workspace/src
