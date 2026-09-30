# models

Typed schemas (pydantic, frozen, `extra="forbid"`) for what LDB reads or persists. Shared vocabulary lives here, behavior does not.

- `task.py`: `Task` (a validated `task.yaml`), `Problem`, existing-library config, plus the filesystem checks Harbor needs.
- `conditions.py`: library conditions (`NoLibrary`, `AuthoredArtifact`, `ExistingLibrary`).
- `job.py`: `Arm` and the requests `Job` (Evaluation Phase), `AuthorJob` (Design Phase), `ReplayJob`.
- `experiment.py`: `ExperimentConfig`, the published setup one `ldb run` executes; loads config YAML and applies `KEY=VALUE` overrides.
- `manifest.py`: `RunManifest`, the persisted request and provenance of one run.
- `reports.py`: per-trial evidence (`TrialReport`, `RunReport`) and `LdbResult`, the schema of `ldb-result.json`.

Owns the schemas of task, config, manifest and result files. `runs` owns their location and store-side sidecars (`manifest.json`, `RunOutputConfig`), plans and run directories; `harbor` and `pipeline` run trials; `reports` rebuilds and aggregates results; `metrics` does cost and metric math; `cli` parses arguments. Imports only Harbor types and `common`.
