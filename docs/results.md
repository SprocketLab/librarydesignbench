# Understanding results

How to read a finished run directory. Run directories live under `runs/` (gitignored). Vocabulary is in the root `AGENTS.md`.

## Run shapes

| Command | Root (default) | Shape |
|---|---|---|
| `ldb run CONFIG` | `runs/agent_attempts/run_<timestamp>/` | Experiment: the root is the Design Run; the Evaluation Run is nested in `evaluation_results/`. |
| `ldb eval design\|no-library\|existing-library` | `runs/evaluation_<timestamp>/` | Evaluation Run: trial dirs sit directly in the root. |
| `ldb verify run SOURCE` | `<source>/verify` (`--output` changes it) | Replay of saved artifacts against current verifiers. |

Timestamps use local time, `%Y-%m-%d__%H-%M-%S`. `--name` replaces the default directory name.
A directory is a run if it has `manifest.json`. `ldb-config.json` and `config.json` are also written per run.

```
<experiment root>/                 # Design Run
  manifest.json                    # what the run measures; resume re-plans from it
  ldb-config.json                  # execution identity: job_id, started_at, trial_count, n_concurrent
  ldb-result.json                  # THE result; written only at the result owner
  config.json  result.json         # Harbor-shaped job config / job result
  run.log                          # JSONL orchestration log
  prompts/<task>__author.md        # rendered design prompts
  sandbox-usage/<trial>.json       # sandbox spend ledger (Modal estimates)
  design_results/<trial>/          # one design attempt each
  evaluation_results/              # nested full run (own manifest, config, prompts, trial dirs)
    <trial>/
```

An Evaluation Run has the same root files, `prompts/<task>__<implementor>__<setup>.md` (implementor label and setup as below), and trial dirs directly under the root.
`ldb-result.json` is written only at the result owner: the experiment root for an experiment, otherwise the run itself.

Transient or hidden entries: `.harbor-*/` (staging while trials run), `build-contexts/` (removed after the run unless `--debug`), `.resume.lock` (writer lease; `--force` replaces a stale one).

## Trial directories (slots)

Evaluation: `{task}-{setup}-a{attempt}__{agent}__{problem}__{hash8}`
- `setup` is the library condition (the `library` field's last segment): `no-library`, `a<N>` for the library from design attempt N, or the existing library's name.
- `attempt` is the evaluation attempt (1-based).
- `agent` and `problem` are lowercased and truncated to 16 characters; `task` and `setup` are lowercased, not truncated.
- `hash8` includes the run timestamp, so the same cell in two runs never shares a name. Truncation means the hash is the only exact key.

Design: `<task>__phase-1__author__a<N>` (lowercased; long names are shortened with a hash), under `design_results/`.

To locate a cell, do not parse names. Filter `ldb-result.json` `.trials[]` on `task`, `problem`, `library`, `implementor`, `attempt` and take `.name`. Or grep `trial-report.json` files for the same fields.

## Files LDB writes

**`ldb-result.json`** (schema_version 1): keys `id`, `meta`, `implementors`, `libraries`, `trials`.
- `meta`: `type` (design | evaluation | replay | experiment), `started_at`, `finished_at`, `repo_commit`, `tasks_hash`, `prompt_paths`, `source_run` and `mode` (replay only: verify | remeasure), `complete`, `outcome_counts`.
- `implementors`: label -> agent, model, reasoning, kwargs (credentials stripped). Label is `<agent>__<model>`.
- `libraries`: key -> `type` (authored | existing | no-library), `task`. Keys: `<task>/a<N>`, `<task>/existing/<name>`, `<task>/no-library`. Authored entries add `path` (the author's workspace, relative to the run dir), `source_run`, `attempt`, `author` (agent details) and, for design runs, `score`, tokens, `elapsed`, `steps`, `cost`, `incomplete_reason`. Same-numbered attempts from different runs get an `@<hash8>` key suffix.
- `trials[]`: `name`, `task`, `attempt` (evaluation attempt), `problem`, `library` (key), `implementor`, `outcome`, `simplicity`, `pass_rate`, `score`, token counts, `elapsed`, `steps`, `cost`, `sandbox_cost`, `incomplete_reason`. `score` is set only when `outcome` is `finished`.
- It holds no standard errors or confidence intervals; those are computed from `trial-report.json` files when the console tables are printed.

**`trial-report.json`** (per slot): `trial_name`, `implementor`, `task`, `problem`, `library`, `agent`, `model`, `reasoning`, `attempt`, `design_attempt`, `prompt_path`, `environment_type`, `library_kind`, `status`, `reward`, `pass_rate`, `verifier_rewards`, `usage`, `sandbox_usage`, `static_analysis`, `incomplete_reason`, `reference_library`, `simplicity`, `score`, `simplicity_ratios`.
- `usage`: `input_tokens` (includes cache reads), `cache_input_tokens`, `output_tokens`, `cost_usd`, `reported_cost_usd`, `standardized_cost_usd`, `time_spent`, `agent_steps`.
- `sandbox_usage`: `cost_usd` and per-`attempts[]` provider intervals. Modal estimate; docker is 0.0.
- `status`: `passed`, `failed`, `completed` or `unknown`. `verifier_rewards` is the whole `verifier/reward.json`.
- Design trials: `pass_rate` and `static_analysis` are null; the reward is 1.0 if the authored library passes the readiness script, else 0.0.
- `library_kind` values: `author` (design trial), `agent` (an authored library), `existing`, `no-library`.

**`manifest.json`**: `repo_commit`, `tasks_hash`, `timestamp`, `request` (`kind` `job` = evaluation, `author` = design, or `replay`; carries the problems or tasks, library conditions or design agent, `attempts`, `environment`, `n_concurrent`, and for `job` `verifier_env`), optional `experiment` (overrides, config, design agent), optional `verification` (provenance of the verifier results now stored, written by a verifier replay merged back into the run). It is the authority on what the run does; changed task sources only warn (`tasks_hash`).

**Other slot files**: `limit.json` (`kind`: cost | timeout | none), `sandbox-usage.json` (slot copy of the ledger), `agent-error.json` (only if the agent phase raised), `trial.log`, `retries/<n>/` (archived earlier executions, only after a retry).

**`run.log`** is orchestration diagnostics. Per-trial detail is in the slot's `trial.log`.

## Files Harbor writes in a slot

| Path | Contents |
|---|---|
| `result.json` | Harbor TrialResult: `agent_result` (tokens, cost), `verifier_result.rewards`, timing, `exception_info` |
| `config.json` | Harbor trial config |
| `lock.json` | Harbor's resolved trial spec: task digest, agent, environment, verifier |
| `agent/` | `trajectory.json` plus agent-specific logs (e.g. `codex.txt`, `claude-code.txt`) |
| `artifacts/` | collected files: `workspace/` is the agent's final `/workspace` (a design slot's is the authored library); `manifest.json` lists each collected `source` and its `destination` |
| `verifier/` | `reward.json`, `behavior.json` (passed/total), `static_metrics.json`, `test-stdout.txt`; plus whatever the task's tests write (e.g. `ctrf.json`); on failure `format.log` or `measure.log` |

Where to look:
- Trajectory: `agent/trajectory.json`.
- What the agent built: `artifacts/workspace/`.
- Why tests failed: `verifier/test-stdout.txt`, `verifier/ctrf.json`.
- Reward: `verifier/reward.json` `.reward`; Harbor records the same document as `result.json` `.verifier_result.rewards`, and LDB copies it to `trial-report.json` `.verifier_rewards` and `.reward`.
- Authored library used by a cell: `ldb-result.json` `.libraries["<task>/a<N>"].path`.
- No `static_metrics.json` means the trial is unmeasured.

## Reading the scores

The reward is computed by the task verifier (in the ldb-tasks repo), not by this repo.

| Metric | Meaning | Better |
|---|---|---|
| `pass_rate` | passed / total behavioral tests, 0..1 | higher |
| static metrics | `stmts`, `sloc`, `cog_complex`, `cyc_complex`, `halstead_volume`, `parse_tokens` of the formatted workspace | lower = simpler code |
| `simplicity_ratios` | per metric `max(ref,1) / max(actual,1)` against the problem's `tests/static_reference.json` (see `reference_library`) | higher; above 1 is simpler than the reference |
| `simplicity` | mean of the four score-metric ratios (`cyc_complex`, `cog_complex`, `halstead_volume`, `sloc`), uncapped, can exceed 1 | higher |
| `score` (= `reward`) | `pass_rate^2 x` mean of the four ratios each capped at 1.0; range 0..1 | higher |
| `cost`, `sandbox_cost`, `elapsed`, `steps`, tokens | spend and effort | lower is cheaper, no quality direction |

`incomplete_reason` non-null (unmeasurable, missing verifier result, library unavailable) counts as score 0 in aggregates and is excluded from pass-rate and simplicity aggregates.

Uncertainty (`metrics/uncertainty.py` only): the score is a stratified mean. Each task is a fixed stratum, each independent library run (design attempt for authored libraries, evaluation attempt for existing and no-library) is a replicate, and tasks are weighted equally. The 95% CI describes reruns of this fixed benchmark, not new tasks. It needs at least 2 runs per task with balanced coverage; the console prints `mean [lo, hi]`, or `mean (no rerun variation observed)`. Other measures use a task-clustered mean.

Cost: `cost_usd` is the effective cost. `reported_cost_usd` is what the provider reported; `standardized_cost_usd` is repriced from tokens (see `ldb recalculate` below).

How to compare: score every implementor on the same problems under `no-library` (floor), `existing` (comparator) and authored libraries. An authored library helps when its score beats the floor; it is competitive when it approaches the existing library. Compare like with like: same implementor, same problems.

## Outcome classes

Each slot is classified `finished`, `reanalyze`, `reverify` or `rerun`; `meta.outcome_counts` tallies them and `meta.complete` means all are `finished`.
- `finished`: graded fairly, including productive cost-limit or timeout stops.
- `rerun`: no valid result or non-limit exception; the agent must run again.
- `reverify`: agent work survives but grading failed; regrade the saved workspace.
- `reanalyze`: graded, but static measurement was never attempted; remeasure the saved workspace. A *stale* measurement (older `MEASUREMENT_REVISION` or a changed static reference) still classifies as `finished`, but `ldb resume` remeasures it too.

## What later commands change

Derived documents are always regenerated, never patched: every `trial-report.json`, run-root `result.json` and `ldb-result.json` (a stale `ldb-result.json` is deleted first).
- `ldb resume RUN_DIR` / `ldb run EXPERIMENT_DIR`: reruns `rerun` slots, replays `reverify` and `reanalyze` (and stale) slots, then rebuilds reports. Mechanism and scope: [runner.md](runner.md#5-durability-and-resume).
- `ldb recalculate PATH`: remeasures saved workspaces with the current verifier and rewrites reports. `--input-cost`, `--output-cost`, `--cache-input-cost`, `--implementor` and `--pricing-config` reprice `standardized_cost_usd`.
- `ldb verify run SOURCE`: a replay run (`meta.type` "replay") in `<source>/verify` that reuses the source trial names. Token, elapsed and cost fields are omitted. `--update` replaces the source's verifier results after success.
- `ldb static DIRECTORY`: remeasures every task's static reference; it does not touch runs.
