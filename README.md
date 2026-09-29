# lib-design-bench

`lib-design-bench` measures whether a library makes fresh downstream agents build
real application work with less implementation burden than they would carry
without it.

Each task has two phases:

1. **Design Phase** — an author creates a reusable library at `/workspace` from
   the complete `design/instruction.md` specification.
2. **Evaluation Phase** — fresh implementors solve independent native problems
   under a library condition. Authored Design Phase artifacts mount read-only
   at `/library`; no-library has no library, and existing libraries are
   installed in the task image.

The Evaluation Phase is the instrument. Verifier reward establishes behavioral
validity; checked-in static references quantify how much downstream
implementation burden each library condition carries. No-library and
existing-library conditions provide experimental context.
