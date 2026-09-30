# harbor

The only module that talks to Harbor: turns launches into running trials.

- Materializes a build context (`build-contexts/<build_context_key>/` in the run dir, deleted after the run unless `ldb run --debug`) shared by every trial with that key, and one Harbor trial config per trial. The library condition (`NoLibrary`, `ExistingLibrary`, `AuthoredArtifact`) and language policy decide what gets installed.
- Runs trial batches: retry policy, live progress, batch diagnostics.
- Owns the Harbor agents and environments LDB plugs in by import path: workspace setup wrapper around the selected agent, artifact replay agent, Modal/Docker environment adapters.
- Owns sandbox spend accounting: writes `sandbox-usage.json` in each trial dir and a copy in the run-root `sandbox-usage/` ledger (path constants live in `runs/`).

Not here: what a launch is (`models/`), which launches a run plans and the run directory layout (`runs/`, `pipeline/`), scoring (`metrics/`), reports (`reports/`), CLI flags (`cli/`).
