# cli

The `ldb` command line: the boundary between a person's arguments and the pipeline.

- Owns the commands `run`, `eval` (`design`, `no-library`, `existing-library`), `resume`, `recalculate`, `static`, and `verify` (`task`, `run`), plus option parsing, `KEY=VALUE` config overrides, task/problem selectors, and agent/environment flags.
- Owns console output: result tables (`display.py`) and printing the saved result JSON (`common.py`).
- Coercion and normalization of user input ends here; downstream modules get typed, validated values.
- Orchestration only: planning and execution live in `pipeline/`, run state in `runs/`, Harbor wiring in `harbor/`, schemas in `models/`, report building in `reports/`.
- Score and cost estimation belongs to `metrics/` and `reports/`; `display.py` only formats what they return.
