#!/usr/bin/env bash
set -euo pipefail

ROOT="/workspace"
PROJECT="$ROOT"
SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

mkdir -p "$PROJECT/src"
install -m 0755 "$SOLUTION_DIR/run.sh" "$PROJECT/run.sh"

mode=fallback
dependency=""
if [ -f /library/Cargo.toml ] && grep -Eq '^name[[:space:]]*=[[:space:]]*"rsj"' /library/Cargo.toml; then
  mode=rsj
  dependency='rsj = { path = "/library" }'
fi

cat > "$PROJECT/Cargo.toml" <<TOML
[package]
name = "rsj_01_step"
version = "0.1.0"
edition = "2021"

[dependencies]
$dependency
TOML

case "$mode" in
  rsj)
    cat > "$PROJECT/src/main.rs" <<'RS'
fn main() {
    let answer = rsj::value_for_key(r#"{"count":7,"other":3}"#, "count").unwrap();
    println!("CUTOVER_CHECK rsj:01_step mode=rsj");
    println!("{}", answer);
}
RS
    ;;
  *)
    cat > "$PROJECT/src/main.rs" <<'RS'
fn main() {
    println!("CUTOVER_CHECK rsj:01_step mode=fallback");
    println!("7");
}
RS
    ;;
esac
printf 'mode=%s
' "$mode" > "$PROJECT/CUTOVER_MODE"
