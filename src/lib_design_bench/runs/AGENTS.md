# runs

Persisted run directories: where a run's files live and what resuming owes each trial.

- `store.py` defines the on-disk layout: `manifest.json` (the request authority), `ldb-config.json`, `design_results/` (one slot per design attempt) beside the nested `evaluation_results/`, per-trial `result.json` (Harbor's) and `trial-report.json`, and the `sandbox-usage/` ledger. It expands a request into trial launches and writes run output documents atomically.
- `trial-report.json` content is written by `reports/` and `pipeline/`; this module only names and reads it.
- `plan.py` creates or reopens a run from its own manifest, renders prompts, and grows an evaluation run from settled design tasks.
- `outcomes.py` classifies each slot as `finished`, `reanalyze`, `reverify` or `rerun`.
- Report and metric content: `reports/`, `metrics/`; schemas: `models/`; launching and Harbor glue: `cli/`, `harbor/`. This module never runs trials.
