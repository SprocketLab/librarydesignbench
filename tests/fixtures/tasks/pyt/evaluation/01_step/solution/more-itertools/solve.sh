#!/usr/bin/env bash
set -euo pipefail

solution_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
install -m 0644 "$solution_dir/main.py" /workspace/main.py
install -m 0755 "$solution_dir/run.sh" /workspace/run.sh
