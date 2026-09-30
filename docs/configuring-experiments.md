# Configure an experiment

`ldb run` reads a YAML config for the tasks, attempt counts, implementors, prompts,
and sandbox sizes. Start with [the official config](../configs/experiments/official.yaml)
or save this smaller example as `configs/experiments/clirs-example.yaml`:

```yaml
version: "1.0.0"
tasks: [clirs]
design:
  attempts: 1
  sandbox: {cpus: 4, memory_mb: 8192, storage_mb: 10240}
evaluation:
  agents:
    luna:                          # selector key: --eval-agent luna
      name: codex                  # Harbor agent
      model_name: gpt-5.6-luna
      kwargs: {reasoning_effort: high, version: "0.153.4"}
  attempts: 1
  prompt: ../prompts/library_use_inst.md
  sandbox: {cpus: 2, memory_mb: 4096, storage_mb: 10240}
```

From the runner repo, with access to the chosen model providers:

```bash
uv run ldb run configs/experiments/clirs-example.yaml \
  -a claude-code -m anthropic/claude-sonnet-5-5 --reasoning high \
  --problem maintainer-tools
```

This authors **one `clirs` library** and evaluates it on one selected problem
with one implementor and one evaluation attempt. The author comes from `-a` and
`-m`, not `evaluation.agents`; the latter lists the **implementors**. The
`--problem` filter applies only to this invocation's Evaluation Phase; the
saved experiment still has all active `clirs` problems available. Repeat
`--problem maintainer-tools` when continuing the experiment or the other
problems become eligible to run. `--task` when starting from a config selects
which tasks are persisted; `tasks: [clirs]` already does that here. See
[How the runner works](runner.md#2-ldb-run) for design and resume scope.

## Paths and overrides

- In a config, `design.prompt` and `evaluation.prompt` are **file paths relative
  to the config file**. The example lives in `configs/experiments/`, so
  `../prompts/library_use_inst.md` points to `configs/prompts/`. Prompt
  templates must contain `{{instruction}}`; other supported variables are
  rendered per task and library condition. Omit `design.prompt` for the task's
  raw design instruction. `ldb eval --prompt`, by contrast, takes a CLI path
  relative to the current directory.
- To use a local task checkout instead of LDB's pinned checkout, add
  `tasks_root: ../ldb-tasks` to the config **when running from this repo's
  root**. Unlike prompt paths, a relative `tasks_root` resolves against the
  current working directory, not the config directory. Use an absolute path
  if you run from elsewhere.
- Trailing `KEY=VALUE` arguments override config keys before validation:
  `design.attempts=2` or
  `evaluation.agents.luna.model_name=gpt-5.6-luna`, for example. They are
  recorded with the resolved config in the manifest. There is no `problems`
  config key; use `--problem` for invocation-local selection.
- Docker is the default environment. In YAML, set
  `environment: {type: modal}` (or `daytona`); on the CLI, append the trailing
  override `environment.type=modal`. Credentials belong in provider-native
  environment variables and
  agent keys in `--agent-env`, **not** in this YAML. See [Setup](../README.md#setup)
  for the resume implication of agent environment variables.

The config and CLI model flags shape a **new** experiment; continuing its
run directory uses the saved manifest and will reject author/credential flags.
On continuation, use selection flags such as `--problem` and `--eval-agent`
(within the saved scope) to choose work for that invocation; environment and
concurrency overrides are also available. See [Quick start](../README.md#quick-start)
for a continuation example.
