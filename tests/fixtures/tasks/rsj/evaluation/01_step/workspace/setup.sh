#!/usr/bin/env bash
set -euo pipefail

CARGO_HOME_DIR="${CARGO_HOME:-${HOME:-/root}/.cargo}"
printf 'CUTOVER_SETUP rsj cargo_offline=1 cargo_home=%s
' "$CARGO_HOME_DIR"
