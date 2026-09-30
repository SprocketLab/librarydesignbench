# metrics

Pure statistics and cost arithmetic over already-loaded results. Only IO: reading one model-pricing YAML.

- `uncertainty.py` is the only place a standard error, deviation or confidence interval of a benchmark measure is computed.
  - `estimate_score` / `stratified_mean`: the benchmark score. Task = fixed stratum, library run = replicate, tasks weighted equally. The CI is for reruns of this fixed benchmark, not for new tasks.
  - `clustered_mean`: descriptive non-score measures.
  - Score library kinds (`agent` = authored, `existing`, `no-library`) and replicate identity are normalized here; readers must not re-derive them.
- `costs.py` owns pricing rates (`CostRates`, `ModelPricing` YAML, `ImplementorPricing`) and repricing a trial's `UsageReport` from retained token counts, keeping the reported cost alongside the standardized one.
- Callers: `reports/` (score tables, repricing), `pipeline/` (selects rates during replay), `cli/` (display, pricing flags). Run layout and result reading live in `runs/`, `reports/`. Metrics receive plain values.
