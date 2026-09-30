# Compare library conditions on one problem

This walkthrough compares one authored library, an existing library, and no
library on `clirs/maintainer-tools`. Run from the runner repo with Docker and
model access configured. Each command below starts paid agent work; this is a
**workflow demonstration**, not a statistically meaningful benchmark result.
For score definitions and run layout, see [Understanding results](results.md).

## 1. Author one library

```bash
uv run ldb run configs/experiments/official.yaml \
  -a claude-code -m anthropic/claude-sonnet-5-5 --reasoning high \
  --task clirs --design-only -n 1 design.attempts=1
```

Find the resulting `runs/agent_attempts/run_<timestamp>/` directory in the
command output. Set `DESIGN_DIR` to that actual path, for example:

```bash
DESIGN_DIR=runs/agent_attempts/YOUR_RUN_DIRECTORY
```

Check its `ldb-result.json` library entry for `clirs/a1` and the design trial
for a usable authored workspace. `--design-only` does not produce evaluation
scores; the next command evaluates that saved library.

## 2. Evaluate the same implementor on three conditions

All three evaluations use Codex, model `gpt-5.6-luna`, version `0.153.4`, high
reasoning, and **one attempt** for the same problem. Use unique `--name` values
if those run directories already exist. The authored and existing conditions
use the official library-use prompt; the no-library condition must use the
no-library prompt, which does not instruct the agent to use a nonexistent
library. This necessary prompt difference should be reported alongside scores.

```bash
uv run ldb eval design "$DESIGN_DIR" \
  -a codex -m gpt-5.6-luna --agent-version 0.153.4 --reasoning high \
  --task clirs --problem maintainer-tools --attempts 1 \
  --prompt configs/prompts/library_use_inst.md --name compare-clirs-authored

uv run ldb eval existing-library \
  -a codex -m gpt-5.6-luna --agent-version 0.153.4 --reasoning high \
  --task clirs --problem maintainer-tools --existing-library clap --attempts 1 \
  --prompt configs/prompts/library_use_inst.md --name compare-clirs-existing

uv run ldb eval no-library \
  -a codex -m gpt-5.6-luna --agent-version 0.153.4 --reasoning high \
  --task clirs --problem maintainer-tools --attempts 1 \
  --prompt configs/prompts/no_library_inst.md --name compare-clirs-floor
```

Each standalone Evaluation Run has its own result:
`runs/evaluation_<timestamp>/ldb-result.json` (with these `--name` values:
`runs/compare-clirs-authored/ldb-result.json`,
`runs/compare-clirs-existing/ldb-result.json`, and
`runs/compare-clirs-floor/ldb-result.json`). The Design Run's
`$DESIGN_DIR/ldb-result.json` describes authoring, **not** one of the three
evaluation scores.

## 3. Check evidence before comparing scores

The script below reads the one trial in each Evaluation Run and refuses to
compare unfinished or unmeasured trials. `tasks_hash` covers the selected task
source directory, not the library condition; all three runs should use the
same task checkout. Run it from this repo's root:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

runs = {
    "authored": Path("runs/compare-clirs-authored/ldb-result.json"),
    "existing": Path("runs/compare-clirs-existing/ldb-result.json"),
    "no-library": Path("runs/compare-clirs-floor/ldb-result.json"),
}
identities = set()
task_hashes = set()
for condition, path in runs.items():
    result = json.loads(path.read_text())
    task_hashes.add(result["meta"]["tasks_hash"])
    trials = [
        trial for trial in result["trials"]
        if trial["task"] == "clirs" and trial["problem"] == "maintainer-tools"
    ]
    if not result["meta"]["complete"] or len(trials) != 1:
        raise SystemExit(f"{condition}: incomplete run or unexpected trial count: {path}")
    trial = trials[0]
    if trial["outcome"] != "finished" or trial.get("incomplete_reason"):
        raise SystemExit(f"{condition}: {trial['outcome']}: {trial.get('incomplete_reason')}")
    if any(trial.get(key) is None for key in ("pass_rate", "simplicity", "score")):
        raise SystemExit(f"{condition}: missing behavioral or static measurement")
    library = result["libraries"][trial["library"]]
    agent = result["implementors"][trial["implementor"]]
    identities.add((agent["agent"], agent["model"], agent.get("version"), agent.get("reasoning")))
    print(condition, library["type"], "pass_rate=", trial["pass_rate"], "score=", trial["score"])
if len(identities) != 1:
    raise SystemExit(f"Implementors differ: {identities}")
if len(task_hashes) != 1:
    raise SystemExit("Task sources differ between runs")
PY
```

If a check fails, inspect that trial's `trial-report.json` and `verifier/`
files via [the result layout](results.md#where-to-look). Compare `score` only
on matched problems and implementors; `pass_rate` explains behavioral success,
and `simplicity` how much code the implementor needed relative to the reference.
A single attempt on a single problem cannot establish an aggregate improvement
or uncertainty interval. For a real comparison, repeat across the same task
set and attempts and inspect the task-weighted summaries described in
[Reading the scores](results.md#reading-the-scores).
