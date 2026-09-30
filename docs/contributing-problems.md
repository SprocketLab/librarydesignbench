# Contributing problems

The runner lives in this repository; benchmark tasks live in
[`ldb-tasks`](https://github.com/gabeorlanski/ldb-tasks). Write and open the
problem PR **there**. These instructions live here, so they match the runner's
current commands. For the directory contract, see [Task structure](tasks.md).

## Add a problem to a task

Work in a local `ldb-tasks` checkout next to this repository (the commands below
assume `../ldb-tasks`). Do not edit the cached pinned checkout in
`~/.cache/lib-design-bench/tasks`: LDB can replace its contents when the pin
changes. Use an existing problem in the same task as a layout example.

1. Add the new name to the task's `task.yaml` `problems` list. Only listed
   problems are active. If the task declares `existing_library_problems`, assign
   the new problem to exactly one existing library there as well.
2. Create `evaluation/NEW_PROBLEM/instruction.md` with a precise input, output,
   and error-behavior contract. Add its `task.toml` and any neutral
   `workspace/` starter files. The starter must not contain the solution.
3. Write behavioral tests and `tests/behavior.sh` for the public contract,
   including relevant edge cases. Add reference solutions under
   `solution/no-library/` and `solution/<existing-library>/` with runnable
   `solve.sh` scripts. The no-library solution and at least one declared
   existing-library solution must solve the same problem; add the relevant
   existing-library solution(s) for each condition you intend to check.
4. Generate the verifier files and static reference, then verify the references
   from the **runner repo**:

   ```bash
   uv run ../ldb-tasks/_verifier/sync.py ../ldb-tasks
   uv run ldb static ../ldb-tasks/clirs
   uv run ldb verify task clirs --problem NEW_PROBLEM \
     -o runs/reference-check tasks_root=../ldb-tasks
   ```

   Replace `clirs` and `NEW_PROBLEM` with your task and problem. `sync.py`
   updates generated verifier files for **every active problem** in the task
   checkout; commit the regenerated files that change. `ldb static` remeasures
   **all active problems** in the specified task and writes their
   `tests/static_reference.json`; review and commit the intended changes.
   `ldb verify task` runs the oracle's behavioral and static checks on only the
   **reference solutions present** for the selected problem. It does not catch
   an omitted solution. Confirm that both the no-library and expected existing
   arms appear in its JSON summary, pass their behavioral tests, and have
   static measurements. A zero exit status alone is not evidence the intended
   pair was checked. These commands start Docker/Harbor trials.

Do not hand-edit `tests/test.sh`, `tests/static_measure.py`, or formatter
configs: `_verifier/sync.py` regenerates them. If you change `_verifier/`, run
the sync command and review the generated diffs across the checkout. Follow
the task repo's existing canary and pinned-dependency conventions for new files.

## Submit the PR

Include the contract, tests, starter, reference solutions, generated verifier
files and static reference in the `ldb-tasks` PR. In the PR description, list
the checked arms and the `ldb verify task` pass counts, plus any intentional
reference-metric changes outside the new problem. If verification fails,
repair the task before submitting. After a task PR is merged, a maintainer
updates `_TASKS_REVISION` in this runner's
[`common.py`](../src/lib_design_bench/common.py) to include it; editing this
repo alone does not add a problem to the pinned benchmark.
