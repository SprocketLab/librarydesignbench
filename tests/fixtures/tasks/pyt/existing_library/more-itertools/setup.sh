#!/usr/bin/env bash
set -euo pipefail

workspace_path="$1"
printf 'existing-library:more-itertools\n' > "$workspace_path/setup-hook-ran"
