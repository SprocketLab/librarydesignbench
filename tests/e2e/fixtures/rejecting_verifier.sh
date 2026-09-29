#!/usr/bin/env bash
set -euo pipefail

VERIFIER_LOG_DIR="${LDB_VERIFIER_LOG_DIR:-/logs/verifier}"
mkdir -p "$VERIFIER_LOG_DIR"
printf '{"reward": 0.0, "passed": 0, "total": 2}\n' > "$VERIFIER_LOG_DIR/reward.json"
printf '0.0\n' > "$VERIFIER_LOG_DIR/reward.txt"
