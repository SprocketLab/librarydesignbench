# pipeline

Orchestrates whole runs end to end: takes a planned run directory through
launching trials, finalizing the run report, and replaying saved slots.

Owns:
- The run writer lease (`.resume.lock`) that guards a run directory, and the run/finalize lifecycle.
- Replay of saved slots: reverify (re-grade with current tests), remeasure (static analysis without behavioral tests), recalculate (rebuild rewards and reports, optionally repriced).
- `MEASUREMENT_REVISION`, the version of the static-measure and reward contract.
- Refreshing each problem's `tests/static_reference.json`, by measuring its reference solution through the task verifier.

Delegates, does not reimplement:
- Planning and on-disk run/slot layout: `runs/`. Trial materialization and Docker execution: `harbor/`.
- Report and result documents: `reports/`; cost math: `metrics/`; data models: `models/`.
- Static analysis and grading live in each task's verifier (`ldb-tasks`), never here. Argument parsing and display: `cli/`, the only caller.
