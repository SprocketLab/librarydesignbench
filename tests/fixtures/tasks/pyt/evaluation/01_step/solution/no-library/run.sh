#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [ -f /workspace/.venv/bin/activate ]; then
  source /workspace/.venv/bin/activate
fi
python3 "$PROJECT_DIR/main.py"
