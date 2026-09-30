# reports

Derives the persisted report documents from Harbor evidence and run state: `trial-report.json` per slot, then one `ldb-result.json` at the result owner, plus the experiment view the console shows.

Owns:
- Turning a Harbor `TrialResult` into a `TrialReport` (status, issues, usage, static metrics, score).
- Run-level rollups: `RunReport`, the `LdbResult` envelope, per-library and per-implementor aggregates, score spreads.
- The rebuild path (`rebuild.py`): every derived document is regenerated through it, never patched in place. It unpublishes the stale `ldb-result.json` (removal itself is `runs/store.py`) before trial reports are rewritten.

Not here:
- Document schemas and `ldb-result.json` name: `models/reports.py`. Slot and run storage layout, the `trial-report.json` name, atomic writes: `runs/store.py`. Outcome classification: `runs/outcomes.py`.
- Cost rates, standardization, statistics: `metrics/`. Running trials and replays: `harbor/`, `pipeline/`. CLI rendering: `cli/`.
