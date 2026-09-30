# Library Design Bench (LDB)

LDB measures how well an agent can design a library. An author agent writes a library for a task (Design Phase), then fresh implementor agents solve downstream problems with it (Evaluation Phase), and the result is scored on passing tests and on how little code they needed. The Python package `lib_design_bench` (CLI `uv run ldb`) runs this on the external Harbor framework.

## Terminology

Use these words exactly; they match the code and persisted files.

### Benchmark structure
- **Task**: a directory with `task.yaml`, from the separate `ldb-tasks` repo (pinned commit, cached in `~/.cache/lib-design-bench/tasks`; `tasks_root` selects another checkout). Holds `design/`, shared `environment/`, `evaluation/<problem>/`, `existing_library/<name>/`.
- **Problem**: one Evaluation Phase Harbor task, `evaluation/<name>/`, belonging to a task. Select with `--problem NAME` or `TASK/NAME`.
- **Design Phase**: an author agent writes a library into `/workspace`; the collected workspace is the authored library.
- **Evaluation Phase**: implementors solve each problem with a library condition available at `/library`.
- **Harbor task**: a directory with `task.toml`. A task yields one for design and one per problem.

### Conditions and agents
- **Library condition**: what an implementor gets. `no-library` (the floor), `authored` (Design Phase output, named `a<N>` for design attempt N), `existing` (pinned production library declared by the task, also called comparator).
- **Spine**: the task's default existing library; `ldb eval existing-library` uses it unless `--existing-library` is given.
- **Author / design agent**: the agent in the Design Phase (`--agent` on `ldb run`). Trials and reports label it `author`.
- **Implementor**: an agent+model solving problems in the Evaluation Phase. Config and CLI say "eval agent" (`evaluation.agents.<key>`, `--eval-agent`); reports say `implementor`. Its label is `<agent>__<model>`.
- **Arm**: one implementor + one library condition.
- **Floor / ceiling**: in `ldb verify task`, the checked-in `no-library` reference solution and the existing-library reference solutions.
- **Cell**: implementor x problem x library condition. Each cell has one or more evaluation attempts.
- **Attempt** is overloaded. *Design attempt* (`design.attempts`, `a<N>`, `design_attempt`) versus *evaluation attempt* (`evaluation.attempts`, `--attempts`, `-a<N>` in trial names). Always say which.

### Execution and results
- **Experiment**: one `ldb run`: a Design Run plus its Evaluation Run, driven by an experiment YAML (`configs/experiments/`). The experiment directory is the design run's directory; the evaluation run is nested in `evaluation_results/`.
- **Design Run / Evaluation Run**: a run directory of one type. `ldb eval ...` makes a standalone Evaluation Run.
- **Run**: a directory with `manifest.json`. The manifest's request is the authority on what the run does; resume replans from it, not from CLI arguments.
- **Trial**: one agent execution plus verification. **Slot**: the durable directory of one trial (`result.json`, `trial-report.json`, `agent/`, `verifier/`, `artifacts/`).
- **Outcome class** (what resume owes a slot): `finished`, `rerun` (relaunch the agent), `reverify` (regrade saved work; some code says "regrade"), `reanalyze` (remeasure static metrics only).
- **Replay**: re-run the current verifier over saved artifacts with no LLM (`ldb verify run`, `ldb recalculate`, `ldb static`). Modes: `verify` (full tests) and `remeasure` (behavioral tests skipped, recorded pass counts reused).
- **Result owner**: the run that publishes `ldb-result.json`: the experiment root for experiments, otherwise the run itself.
- **Environment** (Harbor provider `type`, default docker) versus **sandbox** (cpus, memory, storage sizing). Provider spend is `sandbox_cost`.
- **Lease**: `.resume.lock`, the exclusive writer lock on a run directory; `--force` replaces a stale one.
- **Build context**: the materialized Harbor task directory shared by every launch with the same key. Removed after a run unless `--debug`.

### Measurement
- **Behavioral tests** give `pass_rate` (passed/total). **Static metrics** are six scalars measured on the formatted workspace: `stmts`, `sloc`, `cog_complex`, `cyc_complex`, `halstead_volume`, `parse_tokens`.
- **Static reference**: per-problem `tests/static_reference.json`, the metrics of one reference solution (`no-library` or a declared existing library). The denominator for ratios.
- **Ratio** `ratio.<metric>` = reference / actual; above 1 means simpler than the reference.
- **Simplicity**: the mean of the ratios of `cyc_complex`, `cog_complex`, `halstead_volume`, `sloc`. The verifier's `simplicity` is uncapped; reports aggregate the capped version (each ratio at most 1.0).
- **Reward / score**: `score = pass_rate^2 x capped simplicity`, range 0 to 1, higher is better. `score` is the reported name; `reward` is the verifier's key. An `incomplete_reason` counts as score 0 and is left out of pass-rate and simplicity aggregates.
- **Design trial reward**: 1.0 or 0.0 for whether the authored library installs and passes the LDB readiness check. No simplicity, no pass rate.
- **Spread**: `mean`, `se`, `n`, and 95% CI (`ci95_low`, `ci95_high`). Score is a stratified mean: task is a fixed stratum, one independently executed library condition is a replicate, tasks weigh equally. The CI applies to reruns of this fixed benchmark only.

## Repo map
- `README.md`: how to run each benchmark, result layout, links.
- `docs/runner.md`: how the runner works end to end (plan, execute, resume, replay); `docs/results.md`, `docs/tasks.md`: result files and task layout.
- `configs/`: experiment YAML, prompt templates (Jinja, must contain `{{instruction}}`), `pricing.yaml`, mini-swe-agent template.
- `src/lib_design_bench/cli/`: the `ldb` commands, [AGENTS.md](src/lib_design_bench/cli/AGENTS.md).
- `src/lib_design_bench/pipeline/`: run lifecycle, replays, [AGENTS.md](src/lib_design_bench/pipeline/AGENTS.md).
- `src/lib_design_bench/runs/`: run directory layout, planning, slot classification, [AGENTS.md](src/lib_design_bench/runs/AGENTS.md).
- `src/lib_design_bench/harbor/`: the only code that talks to Harbor, [AGENTS.md](src/lib_design_bench/harbor/AGENTS.md).
- `src/lib_design_bench/reports/`: derives `trial-report.json` and `ldb-result.json`, [AGENTS.md](src/lib_design_bench/reports/AGENTS.md).
- `src/lib_design_bench/models/`: frozen pydantic schemas, [AGENTS.md](src/lib_design_bench/models/AGENTS.md).
- `src/lib_design_bench/metrics/`: statistics and cost repricing, [AGENTS.md](src/lib_design_bench/metrics/AGENTS.md).
- `src/lib_design_bench/common.py`, `logging.py`: task-checkout pinning, trial naming, run logging.
- `tests/`: pytest suite; `tests/fixtures/tasks/{pyt,rsj}` are fake tasks (python, rust).
- `scripts/`: one-off migrations and the formatter installer.
- `runs/`: gitignored run output. `scratch/`: untracked design notes; verify against code before trusting.

## Working rules
- Run everything through `uv run ldb`; tests through `uv run pytest`.
- Docker-marked tests skip without a daemon; `slow_e2e` tests run only with `--run-slow-e2e`.
- Static measurement belongs to the task verifiers in `../ldb-tasks/_verifier`. LDB never computes metrics on the host; it remeasures only by replaying saved workspaces through Harbor. After changing ldb-tasks, bump the pinned revision in `common.py`.
- Never hand-edit a problem's `tests/test.sh`, `static_measure.py` or formatter config; they are generated from `_verifier` by `sync.py`.
- Old-format runs (from `../lib-design-bench`) are converted with `scripts/migrate_old_runs.py`. `src/` and `tests/` carry no backwards-compatibility code. That repo's vocabulary and docs are stale; do not copy from it.
- Derived documents are always regenerated by the rebuild path, never patched by hand.
- Work on `main`; it is the only branch.
