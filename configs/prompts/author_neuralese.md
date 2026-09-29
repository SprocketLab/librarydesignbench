{{instruction}}

## Who this library is for

Humans do not need to understand this library at all, only agents. Fresh coding agents. Each one gets a task in this domain, has `{{library_name}}` installed with its docs, and is scored on how few tokens of code it writes on top of the library while passing hidden tests. The library is a channel between you and that agent: you compress the domain into an API, the agent decompresses its task into a handful of calls. Every token the agent still has to write is a token your channel failed to carry.

Design in **neuralese**: the form two models would settle on if they only had to talk to each other. Optimise token economy for a model, not legibility for a person. Judge every design choice by one question: *what would an agent prefer?*

## What an agent prefers

- One verb per intent that carries the whole task: parse this document, resolve these references, render that report. A model states its intent in one line and wants one call that matches it.
- Names that are the intent, arguments that are the task's own nouns, results that are the shape the task asked for.
- Defaults that already match the common case, so the common program has no configuration at all; the uncommon case is one keyword away.
- Edge cases, validation, ordering, formatting and diagnostics inside the call. The agent writes the happy path and gets the correct program.
- Dense examples over prose. A model finds the example nearest its task and copies it; it reads reference docs only when no example fits.
- Big flat surfaces over layers. Human decomposition, small composable pieces, builders, class hierarchies, configuration objects and abstractions earn a place only where the agent's program gets shorter with them than without.

## Workflow

1. **Write the consumer's programs first.** From the example usages above and the tasks a library of this kind exists for, write ten to fifteen distinct downstream tasks as the program a model would most want to write, in `/workspace/SKETCHES.md`: three to eight lines each, calling functions that do not exist yet. Done when the set spans input parsing, the core operations, output shapes and error paths, and no sketch holds a loop, branch or helper the library could own.
2. **Design the API from the sketches.** Every function a sketch calls is public API with that name and signature. Done when each sketch type-checks against the design.
3. **Implement**, using only these dependencies: {{libraries}}.
4. **Ask the agents.** If you can spawn subagents, do it: for each sketch, hand a subagent only the downstream task text plus the library as installed and its README, with no memory of your design, and have it write the program. Where its program is longer than the sketch, guesses a name wrong, or has to read reference docs, fix the library, then ask again. If you cannot spawn subagents, do the same from a clean context yourself, task text and README only. Done when a fresh agent lands on the sketch without help, for every sketch.
5. **Freeze the examples.** Each sketch, unchanged, becomes a runnable file under `/workspace/examples/` and runs on realistic input. Done when every example runs.
6. **Write `/workspace/README.md`** for the consumer: the examples first, each with one line naming the task it solves, then the reference, then packaging.
7. **Package** as the task instructions specify, build offline, and submit.
