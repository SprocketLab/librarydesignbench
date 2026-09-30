# Understanding results

How to read a finished run directory. Run directories live under `runs/` (gitignored). Vocabulary is in the root [AGENTS.md](../AGENTS.md).

## Where to look

| Question | Where |
|---|---|
| What is the result? | `ldb-result.json` at the result owner (experiment root for `ldb run`, otherwise the run root) |
| Which trial is a given cell? | `ldb-result.json` `.trials[]`: filter on `task`, `problem`, `library`, `implementor`, `attempt`; take `.name` |
| Full evidence for one trial | `<trial>/trial-report.json` |
| What the agent did | `<trial>/agent/trajectory.json` |
| What the agent built | `<trial>/artifacts/workspace/` (for a design trial, the authored library) |
| Which authored library a cell used | `ldb-result.json` `.libraries["<task>/a<N>"].path` |
| Why tests failed | `<trial>/verifier/test-stdout.txt`, `<trial>/verifier/ctrf.json` |
| The raw reward | `<trial>/verifier/reward.json` `.reward`. Harbor records the same document at `result.json` `.verifier_result.rewards`; LDB copies it to `trial-report.json` `.verifier_rewards` and `.reward`. |
| Static measurements | `<trial>/verifier/static_metrics.json`; if absent, the trial is unmeasured |
| What the run was asked to do | `manifest.json` |
| Rendered prompts | `prompts/` |

To locate a cell, do not parse trial names; they are truncated. Use `ldb-result.json` or grep `trial-report.json` files for the same fields.

## Run layout

| Command | Root (default) | Shape |
|---|---|---|
| `ldb run CONFIG` | `runs/agent_attempts/run_<timestamp>/` | Experiment: the root is the Design Run; the Evaluation Run is nested in `evaluation_results/`. |
| `ldb eval design\|no-library\|existing-library` | `runs/evaluation_<timestamp>/` | Evaluation Run: trial dirs sit directly in the root. |
| `ldb verify run SOURCE` | `<source>/verify` (`--output` changes it) | Replay of saved artifacts against current verifiers. |

Timestamps use local time, `%Y-%m-%d__%H-%M-%S`. `--name` replaces the default directory name. A directory is a run if it has `manifest.json`.

```
runs/agent_attempts/run_<timestamp>/   # experiment root (ldb run) = Design Run
  manifest.json                        # what the run measures; resume re-plans from it
  ldb-config.json                      # execution identity: job_id, started_at, trial_count, n_concurrent
  ldb-result.json                      # THE result; written only at the result owner
  config.json  result.json             # Harbor-shaped job config / job result
  run.log                              # JSONL orchestration log
  prompts/<task>__author.md            # rendered design prompts
  sandbox-usage/<trial>.json           # sandbox spend ledger (Modal estimates)
  design_results/
    <trial>/                           # one design attempt per slot
  evaluation_results/                  # nested Evaluation Run
    manifest.json  ldb-config.json  config.json  result.json  run.log   # no ldb-result.json
    prompts/<task>__<implementor>__<setup>.md
    sandbox-usage/<trial>.json
    <trial>/                           # one evaluation attempt per slot

runs/evaluation_<timestamp>/           # standalone Evaluation Run (ldb eval)
  manifest.json  ldb-config.json  ldb-result.json  config.json  result.json  run.log
  prompts/<task>__<implementor>__<setup>.md
  sandbox-usage/<trial>.json
  <trial>/                             # one evaluation attempt per slot
```

Transient or hidden entries: `.harbor-*/` (staging while trials run), `build-contexts/` (removed after the run unless `--debug`), `.resume.lock` (writer lease; `--force` replaces a stale one).

## Trial directories (slots)

### Names

| Phase | Name | Location |
|---|---|---|
| Evaluation | `{task}-{setup}-a{attempt}__{agent}__{problem}__{hash8}` | run root, or `evaluation_results/` |
| Design | `<task>__phase-1__author__a<N>` (lowercased; long names are shortened with a hash) | `design_results/` |

- `setup` is the library condition (the `library` field's last segment): `no-library`, `a<N>` for the library from design attempt N, or the existing library's name.
- `attempt` is the evaluation attempt (1-based).
- `agent` and `problem` are lowercased and truncated to 16 characters; `task` and `setup` are lowercased, not truncated.
- `hash8` includes the run timestamp, so the same cell in two runs never shares a name. Truncation means the hash is the only exact key.

### Contents

| Path | Written by | Contents |
|---|---|---|
| `trial-report.json` | LDB | Per-trial evidence; see [below](#trial-reportjson). |
| `limit.json` | LDB | Which configured limit ended the agent: `kind` (cost \| timeout \| none), `detail`. |
| `sandbox-usage.json` | LDB | Slot copy of the sandbox spend ledger. |
| `trial.log` | LDB | Per-trial log. |
| `agent-error.json` | LDB | Only if the agent phase raised. |
| `retries/<n>/` | LDB | Archived earlier executions, only after a retry. |
| `result.json` | Harbor | TrialResult: `agent_result` (tokens, cost), `verifier_result.rewards`, timing, `exception_info`. |
| `config.json` | Harbor | Trial config. |
| `lock.json` | Harbor | Resolved trial spec: task digest, agent, environment, verifier. |
| `agent/` | Harbor | `trajectory.json` plus agent-specific logs (e.g. `codex.txt`, `claude-code.txt`). |
| `artifacts/` | Harbor | `workspace/` is the agent's final `/workspace` (a design slot's is the authored library); `manifest.json` lists each collected `source` and its `destination`. |
| `verifier/` | Harbor | `reward.json`, `behavior.json` (passed/total), `static_metrics.json`, `test-stdout.txt`, plus whatever the task's tests write (e.g. `ctrf.json`); on failure `format.log` or `measure.log`. |

`run.log` at the run root is orchestration diagnostics only; per-trial detail is in the slot's `trial.log`.

## `ldb-result.json`

The user-facing result, written only at the result owner. `schema_version` is 1; null fields are omitted. It holds no standard errors or confidence intervals; those are computed from `trial-report.json` files when the console tables are printed.

| Key | Contents |
|---|---|
| `id` | Result id. |
| `meta` | Run provenance (table below). |
| `implementors` | Label (`<agent>__<model>`) -> `agent`, `model`, `version`, `reasoning`, `kwargs` (credentials stripped). |
| `libraries` | Library key -> library condition (table below). |
| `trials` | One compact row per evaluation cell and evaluation attempt, whatever its outcome class (table below). An experiment lists every cell, including ones whose library is not available yet. |

### `meta`

| Field | Meaning |
|---|---|
| `type` | `design`, `evaluation`, `replay` or `experiment` |
| `started_at`, `finished_at` | Run timing |
| `repo_commit`, `tasks_hash` | Code and task-source provenance |
| `prompt_paths` | Rendered prompts used |
| `source_run`, `mode` | Replay only: the replayed run, and `verify` or `remeasure` |
| `complete` | True when every slot is `finished` |
| `outcome_counts` | Slots per [outcome class](#outcome-classes) |

### `libraries`

Keys are `<task>/a<N>` (authored), `<task>/existing/<name>` and `<task>/no-library`. Same-numbered design attempts from different runs get an `@<hash8>` key suffix.

| Field | Applies to | Meaning |
|---|---|---|
| `type` | all | `authored`, `existing` or `no-library` |
| `task` | all | Task the library belongs to |
| `path` | authored | The author's workspace, relative to the run dir |
| `source_run`, `attempt` | authored | Design Run and design attempt that produced it |
| `author` | authored | Author agent details (same shape as `implementors`) |
| `score` | authored, design runs | Design trial reward (1.0 ready, 0.0 not) |
| `input_tokens`, `output_tokens`, `input_cache_tokens`, `elapsed`, `steps`, `cost`, `incomplete_reason` | authored, design runs | Authoring spend and effort |

### `trials[]`

| Field | Meaning |
|---|---|
| `name` | Trial directory name |
| `task`, `problem` | The cell's task and problem |
| `library` | Key into `libraries` |
| `implementor` | Key into `implementors` |
| `attempt` | Evaluation attempt |
| `outcome` | [Outcome class](#outcome-classes) |
| `pass_rate`, `simplicity`, `score` | See [Reading the scores](#reading-the-scores); every planned trial carries its recorded `score`, whatever its `outcome` |
| `input_tokens`, `output_tokens`, `input_cache_tokens`, `elapsed`, `steps`, `cost`, `sandbox_cost` | Spend and effort |
| `incomplete_reason` | Why the trial could not be scored, if it could not |

## `trial-report.json`

Per-slot evidence (`schema_version` 1). LDB derives it from the slot's Harbor files and rewrites it on every rebuild.

| Field | Meaning |
|---|---|
| `trial_name`, `trial_path`, `trial_uri` | Slot identity and location |
| `task`, `problem`, `library` | The cell |
| `implementor`, `agent`, `model`, `reasoning` | Who ran the trial |
| `attempt` | Evaluation attempt |
| `design_attempt`, `source_design_run` | For an authored library: which design attempt and Design Run produced it |
| `library_kind` | `author` (design trial), `agent` (an authored library), `existing` or `no-library` |
| `reference_library` | The library whose static reference is the ratio denominator |
| `prompt_path`, `environment_type` | Prompt used and Harbor environment |
| `status` | `passed`, `failed`, `completed` or `unknown` |
| `had_error` | Whether Harbor recorded an exception (`result.json` `.exception_info`) |
| `reward`, `verifier_rewards` | The verifier's reward, and the whole `verifier/reward.json` |
| `pass_rate` | Passed / total behavioral tests |
| `static_analysis` | The six static metrics |
| `simplicity_ratios`, `simplicity`, `score` | See [Reading the scores](#reading-the-scores) |
| `incomplete_reason` | Why the trial could not be scored, if it could not |
| `usage` | Agent tokens, cost and effort (table below) |
| `sandbox_usage` | `cost_usd` plus per-`attempts[]` provider charge intervals; a Modal estimate, 0.0 on docker |

For design trials, `pass_rate` and `static_analysis` are null and the reward is 1.0 if the authored library passes the readiness script, else 0.0.

### `usage`

| Field | Meaning |
|---|---|
| `input_tokens` | Input tokens, including cache reads |
| `uncached_input_tokens`, `cache_input_tokens` | Input tokens split into uncached and cache reads |
| `output_tokens` | Output tokens |
| `cost_usd` | Effective cost used by reports |
| `reported_cost_usd` | Cost the provider reported |
| `standardized_cost_usd` | Cost repriced from tokens (see `ldb recalculate` below) |
| `input_cost_per_million`, `output_cost_per_million`, `cache_input_cost_per_million` | Prices used for repricing |
| `time_spent`, `agent_steps` | Effort |

## `manifest.json`

The authority on what the run does; resume re-plans from it. Changed task sources only warn (via `tasks_hash`).

| Field | Meaning |
|---|---|
| `repo_commit`, `tasks_hash`, `timestamp` | Provenance |
| `request` | What to run. `kind` is `job` (evaluation), `author` (design) or `replay`. Carries the problems or tasks, library conditions or design agent, `attempts`, `environment`, `n_concurrent`, and for `job` `verifier_env`. |
| `experiment` | Optional: experiment overrides, config and design agent |
| `verification` | Optional: provenance of the stored verifier results, written when a verifier replay is merged back into the run |

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

How to compare: score every implementor on the same problems under `no-library` (floor), `existing` (comparator) and authored libraries. An authored library helps when its score beats the floor; it is competitive when it approaches the existing library. Compare like with like: same implementor, same problems. See the [one-problem comparison walkthrough](comparing-results.md) for the commands and evidence checks.

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
