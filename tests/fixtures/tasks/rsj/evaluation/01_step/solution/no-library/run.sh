#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cargo run --quiet --manifest-path "$PROJECT_DIR/Cargo.toml"
