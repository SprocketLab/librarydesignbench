{{instruction}}

## Library Rules

Your solution is a thin adapter around `{{library_name}}`. It is judged on how
little code sits on top of the library, so every operation the library can
carry, the library carries.

- `{{library_name}}` is installed. Its source, examples, tutorials, and docs are in `{{library_install}}` (read-only).
- Reach for the primitive that does the whole operation (the parser, validator, pipeline, runner), not its pieces. Importing constants, error types, or small helpers while hand-rolling the operation is not using the library.
- If the library's default behavior differs from the task, configure or extend the library. Reimplementing is the last resort, and only after a search confirms the library lacks it.
- Handle exactly the validation the task describes.
- Other available dependencies: {{libraries}}. Use them for work outside the library's domain.
- You have one hour.

## Workflow

1. **Map the task onto the library.** Read `{{library_install}}` in this order: README and docs, then examples, then grep the source for each concept the task names. Done when every requirement in the task is paired with the library entry point that carries it, or with "not provided" after a search.
2. **Build the program in `/workspace`** by calling those entry points.
3. **Audit.** For each function, loop, branch, and check you wrote, name the library call that replaces it and use that instead, or note why the library lacks it. Done when every remaining hand-written line has a reason.
4. **Run the task's sample inputs.** Done when each produces the described output. Sample runs are enough; skip test suites.
5. Submit.
