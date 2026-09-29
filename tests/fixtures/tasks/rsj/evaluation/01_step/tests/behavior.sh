#!/usr/bin/env bash
set -euo pipefail

mkdir -p /logs/verifier
WORKDIR="${LDB_WORKDIR:-/workspace}"
PROJECT="$WORKDIR"

expected_mode() {
  if [ -f /library/Cargo.toml ] && grep -Eq '^name[[:space:]]*=[[:space:]]*"rsj"' /library/Cargo.toml; then
    printf 'rsj
'
  elif [ -f /library/Cargo.toml ] && grep -Eq '^name[[:space:]]*=[[:space:]]*"itertools"' /library/Cargo.toml; then
    printf 'itertools
'
  else
    printf 'fallback
'
  fi
}

expected="$(expected_mode)"
printf '{"reward": 0.0, "passed": 0, "total": 2}\n' > /logs/verifier/reward.json
printf '0.0\n' > /logs/verifier/reward.txt

if output="$($PROJECT/run.sh 2>&1)" \
  && printf '%s\n' "$output" | grep -qx 'CUTOVER_CHECK rsj:01_step mode='"$expected" \
  && printf '%s\n' "$output" | grep -qx '7'; then
  printf '{"reward": 1.0, "passed": 2, "total": 2}\n' > /logs/verifier/reward.json
  printf '1.0\n' > /logs/verifier/reward.txt
fi
printf '%s\n' "${output:-}"
