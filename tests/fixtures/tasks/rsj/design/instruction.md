# JSON aggregate API

Build the `rsj` Rust crate at `/workspace`.

Publish a crate exposing `add_values(document, left, right) -> Option<i64>` for
adding two integer fields from a JSON document. The crate may use cached
`serde_json`; do not expose a standalone key-extraction helper.
