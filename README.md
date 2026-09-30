# Library Design Bench (LDB)

LDB measures whether a library lets fresh downstream agents do
real application work with less implementation burden than they would carry
without it. Each task has two phases:

1. **Design Phase**: an author agent builds a reusable library at `/workspace`
   from `design/instruction.md`.
2. **Evaluation Phase**: fresh implementor agents solve independent problems
   under a library condition: an authored library (mounted read-only at
   `/library`), no library, or a pinned existing library installed in the task
   image.

Verifier reward establishes behavioral validity. Checked-in static references
quantify how much code each condition needs. No-library and existing-library
runs give context for the authored libraries.

## Setup

```bash
uv sync              # add --extra dev for pytest, ruff, ty
uv run ldb --help
uv run pytest        # docker / slow e2e tests are skipped unless available / requested
```

- **Tasks** live in the separate `ldb-tasks` repo. LDB clones the pinned
  revision into `~/.cache/lib-design-bench/tasks` on first use. To use a local
  checkout instead, set `tasks_root` in the experiment config, or pass
  `tasks_root=../ldb-tasks` as a trailing override to `ldb eval` or `ldb verify task`.
- **Environments** run on Harbor: `environment.type` is `docker` (default),
  `modal`, `daytona`, or any other Harbor environment type. Docker runs need a
  Docker daemon. Override per run with `environment.type=modal`.
- **Credentials**: LDB stores none. Pass agent-side variables (model API keys)
  with `--agent-env NAME=VALUE`; add network hosts with `--allow-agent-host`.
  Remote environments use their Harbor provider's own credentials.

## Running

### Full experiment

```bash
uv run ldb run configs/experiments/official.yaml -a claude-code -m anthropic/claude-sonnet-5-5 --reasoning high
```

Authors one library per task and attempt (Design Run), then evaluates every
implementor in the config on them (Evaluation Run). The design agent comes from
the flags, everything else from the config; override config values with
trailing `KEY=VALUE` (for example `evaluation.attempts=2`). Useful flags:
`--task`, `--problem`, `--eval-agent`, `--design-only`, `--name`, `-n`.

### Evaluate a design run's libraries

```bash
uv run ldb eval design runs/<design-run> -a codex -m gpt-5.6-luna --reasoning high
```

Evaluates one implementor on the libraries authored by a finished Design Run.
Produces an Evaluation Run at `runs/evaluation_<timestamp>/`.

### No-library floor

```bash
uv run ldb eval no-library -a mini-swe-agent -m openrouter/z-ai/glm-5.3-flash --task <task>
```

Evaluates the implementor with no library mounted.

### Existing library

```bash
uv run ldb eval existing-library -a mini-swe-agent -m openrouter/z-ai/glm-5.3-flash --task <task>
```

Evaluates the implementor with the task's pinned existing library (its
`spine` by default; choose others with `--existing-library NAME`).

All `eval` commands share `--attempts`, `--prompt`, `--task`, `--problem`
(`NAME` or `TASK/NAME`), `--name`, `-o`, `-n`, `--json`. Use `--help` on any
command for the full list.

### Supporting commands

| Command | Purpose |
|---|---|
| `ldb resume RUN_DIR` | Rerun failed slots, regrade and remeasure saved ones, rebuild reports. |
| `ldb verify task TASK -o DIR` | Verify a task's checked-in floor and ceiling reference solutions. |
| `ldb verify run SOURCE` | Replay saved artifacts through the current verifiers. |
| `ldb static DIR` | Remeasure the static references of every task under DIR. |
| `ldb recalculate PATH` | Remeasure saved workspaces, rewrite reports, optionally reprice cost (`--pricing-config configs/pricing.yaml`). |

## Result organization

```
runs/agent_attempts/run_<timestamp>/  experiment root (ldb run); also the design run
  manifest.json  ldb-config.json  config.json  result.json  ldb-result.json  run.log
  prompts/  sandbox-usage/
  design_results/<trial>/           one design attempt per slot
  evaluation_results/               nested evaluation run, same layout
    <trial>/                        one evaluation attempt per slot
      trial-report.json  result.json  limit.json  sandbox-usage.json  trial.log
      agent/  verifier/  artifacts/workspace/
runs/evaluation_<timestamp>/        standalone evaluation run (ldb eval)
  <same root files>  <trial>/...
```

| File | Meaning |
|---|---|
| `ldb-result.json` | The user-facing result: run metadata, implementors, libraries, and one compact row per trial. Written at the experiment root for `ldb run`. |
| `trial-report.json` | Per-trial evidence: reward, pass rate, simplicity, static metrics, token and sandbox cost. |
| `manifest.json` | The run's request; what `resume` re-plans from. |
| `verifier/reward.json` | The verifier's reward, `verifier/static_metrics.json` its static measurements. |
| `artifacts/workspace/` | The agent's final `/workspace`; for a design trial, the authored library. |
| `prompts/` | Rendered prompts used for each task and library condition. |

Score is defined in [AGENTS.md](AGENTS.md). See
[docs/results.md](docs/results.md) for every file, field and metric.

## Key links

- [configs/experiments/official.yaml](configs/experiments/official.yaml): the official experiment config
- [configs/prompts/library_use_inst.md](configs/prompts/library_use_inst.md): the official evaluation prompt
- [docs/results.md](docs/results.md): result layout and metrics
- [docs/tasks.md](docs/tasks.md): task structure
- [docs/runner.md](docs/runner.md): how the runner works end to end
- [AGENTS.md](AGENTS.md): shared terminology
- `ldb-tasks`: the task repo (sibling checkout `../ldb-tasks`)
