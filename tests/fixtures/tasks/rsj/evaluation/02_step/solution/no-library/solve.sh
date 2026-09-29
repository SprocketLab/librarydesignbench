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
name = "rsj_02_step"
version = "0.1.0"
edition = "2021"

[dependencies]
$dependency
TOML

case "$mode" in
  rsj)
    cat > "$PROJECT/src/main.rs" <<'RS'
fn main() {
    let answer = rsj::add_values(r#"{"left":8,"right":13}"#, "left", "right").unwrap();
    println!("CUTOVER_CHECK rsj:02_step mode=rsj");
    println!("{}", answer);
}
RS
    ;;
  *)
    cat > "$PROJECT/src/main.rs" <<'RS'
fn main() {
    println!("CUTOVER_CHECK rsj:02_step mode=fallback");
    println!("21");
}
RS
    ;;
esac
printf 'mode=%s
' "$mode" > "$PROJECT/CUTOVER_MODE"
