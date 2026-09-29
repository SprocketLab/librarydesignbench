Write a Rust CLI in `/workspace` that adds `left` and `right`
from `{"left":8,"right":13}`.

Use `rsj` if the Design Phase crate is installed, otherwise use `hex` if it is
available as a direct dependency in the dependency cache input, otherwise use a
self-contained fallback. Print a `CUTOVER_CHECK rsj:02_step mode=<mode>` line
before the numeric result.
