#!/usr/bin/env bash
set -euo pipefail

solution_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
mkdir -p /workspace/src
install -m 0755 "$solution_dir/run.sh" /workspace/run.sh
install -m 0644 "$solution_dir/main.rs" /workspace/src/main.rs
cat > /workspace/Cargo.toml <<'EOF'
[package]
name = "rsj_02_step"
version = "0.1.0"
edition = "2021"

[dependencies]
itertools = { path = "/library" }
EOF
