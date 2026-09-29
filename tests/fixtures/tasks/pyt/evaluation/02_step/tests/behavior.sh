#!/usr/bin/env bash
set -euo pipefail

VERIFIER_LOG_DIR="${LDB_VERIFIER_LOG_DIR:-/logs/verifier}"
mkdir -p "$VERIFIER_LOG_DIR"
WORKDIR="${LDB_WORKDIR:-/workspace}"
PROJECT="$WORKDIR"

expected_mode() {
  if [ -f /library/src/pyt/__init__.py ] || [ -f /library/pyt/__init__.py ]; then
    printf 'pyt
'
  elif /workspace/.venv/bin/python -c 'import more_itertools' >/dev/null 2>&1 \
      || /workspace/.venv/bin/python -c 'import more_itertools' >/dev/null 2>&1; then
    printf 'more_itertools
'
  else
    printf 'fallback
'
  fi
}

expected="$(expected_mode)"
printf '{"reward": 0.0, "passed": 0, "total": 2}\n' > "$VERIFIER_LOG_DIR/reward.json"
printf '0.0\n' > "$VERIFIER_LOG_DIR/reward.txt"

if output="$($PROJECT/run.sh 2>&1)" \
  && printf '%s\n' "$output" | grep -qx 'CUTOVER_CHECK pyt:02_step mode='"$expected" \
  && printf '%s\n' "$output" | grep -qx '\[1, 2, 3, 4, 5\]'; then
  printf '{"reward": 1.0, "passed": 2, "total": 2}\n' > "$VERIFIER_LOG_DIR/reward.json"
  printf '1.0\n' > "$VERIFIER_LOG_DIR/reward.txt"
fi
printf '%s\n' "${output:-}"
