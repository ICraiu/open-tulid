# open-tulid

Open Tulid is a local-first workflow runtime for coordinating coding agents.
It connects a human-readable tracker, usually an Obsidian vault, to executable
workflow definitions, sandboxed worker jobs, model/resource scheduling, and
trusted task transitions.

The short version:

```text
tracker task
  -> workflow transition
  -> isolated worker workspace
  -> completion evidence
  -> trusted Tulid validation
  -> task moves to the next state
```

Tulid exists because asking an LLM to "just work on the repo" is not enough for
serious engineering work. The useful unit is a scoped task with known context,
declared dependencies, required evidence, validation rules, and a deterministic
runtime that decides whether the task is allowed to move forward.

## What Tulid Does

Tulid gives you a way to run AI coding agents beside you without handing them
unbounded control over the project.

It provides:

- A CLI named `tulid`.
- Tracker-backed projects and tasks.
- Workflow definitions with states, task types, transitions, workers,
  validations, artifacts, and operations.
- Runtime scheduling for one project or every configured project.
- Sandboxed execution workspaces for worker jobs.
- Completion submission and replay protection.
- Trusted validation and transition application in Tulid, not in the worker.
- Worker resource leasing so scarce models are not over-scheduled.
- Optional model proxying so workers receive short-lived Tulid credentials
  instead of upstream API keys.
- Event logs, job state, transaction recovery, and vault validation.

## Why This Exists

Most AI coding workflows fail in predictable ways:

- The model receives too much broad context and drifts into planning.
- Work happens outside a declared task boundary.
- A generated summary claims tests passed, but nothing trusted verified it.
- State is updated manually, inconsistently, or by the same agent doing the work.
- Multiple workers compete for the same local model and degrade each other.
- Project-specific tracker details leak through the whole codebase.

Tulid is designed around stricter boundaries:

- The tracker stores human-facing tasks.
- The workflow defines what movement is legal.
- The worker proposes work and submits evidence.
- Tulid validates evidence and applies state transitions.
- Adapters isolate tracker-specific behavior.
- Resource leases isolate scarce worker/model capacity.

The goal is not to replace human engineering judgment. The goal is to make AI
work legible, reviewable, repeatable, and interruptible.

## Core Concepts

### Project

A configured project points Tulid at:

- a tracker location, such as an Obsidian project directory
- the code repository the worker should edit
- the main branch or base reference
- the project `workflow.yaml`

Projects are configured in `~/.tulid/config.yaml`.

### Workflow

Each project owns a `workflow.yaml`. It declares:

- states, such as `Todo`, `SelfReview1`, `Done`
- task types, such as `ImplementationTask`
- workers, such as `opencode` or `codex`
- transitions, such as `ImplementTask`
- required artifacts, changed files, validations, and operations

See [docs/workflow-guide.md](docs/workflow-guide.md) for the workflow format.

### Worker Job

A runnable transition becomes a job. Tulid prepares an isolated workspace,
builds a prompt packet, starts the worker, receives completion evidence, and
then decides whether the transition can be accepted.

Workers do not get to directly move tasks to `Done`. They submit a completion;
Tulid performs the trusted transition.

### Runtime

The runtime scheduler finds runnable tasks, creates jobs, leases resources,
starts workers, records events, and applies accepted transitions.

Runtime commands can target one project with `--project <name>` or operate
across all configured projects when the project is omitted.

### Model Resources

Workers can be attached to named resources. A local model can have capacity `1`
so only one worker uses it at a time. Remote or subscription-backed resources can
have higher capacity.

Tulid can also expose an OpenAI-compatible proxy endpoint to workers while
keeping the real upstream credential outside the worker container.

## Installation

Requirements:

- Python 3.11+
- `uv` for the preferred development/build path
- Docker for containerized workers

Install:

```bash
./install.sh
```

Manual editable install:

```bash
pip install -e .
tulid init
```

Build:

```bash
./build.sh
```

Or:

```bash
uv build
```

## Configuration

Initialize Tulid:

```bash
tulid init
```

This creates `~/.tulid/config.yaml`.

Minimal shape:

```yaml
tracker:
  type: obsidian
  root: /home/me/repo/obsidian

projects:
  Agent:
    path: Agent
    repo_root: /home/me/repo/Agent
    main_branch: main
```

Worker/resource example:

```yaml
runtime:
  worker_types:
    qwen_27b: opencode
  worker_resources:
    qwen_27b: [local-qwen]

resources:
  local-qwen:
    kind: model
    capacity: 1
    proxy: qwen-proxy

model_proxy:
  qwen-proxy:
    kind: openai
    base_url: http://127.0.0.1:8080/v1
    api_key_file: secrets/local-model.key
```

For Codex subscription access, use a subscription proxy entry instead of
passing upstream credentials to the worker:

```yaml
resources:
  codex-subscription:
    kind: model
    capacity: 4
    proxy: chatgpt-codex

model_proxy:
  chatgpt-codex:
    kind: subscription
    auth_home: ~/.codex
    container_auth_home: /root/.codex
```

## Common Commands

Inspect configuration and projects:

```bash
tulid --help
tulid init
tulid project <name>
tulid vault validate
```

List work:

```bash
tulid tasks list
tulid tasks list <project>
tulid tasks runnable
tulid tasks runnable <project>
```

Run the scheduler:

```bash
tulid runtime start
tulid runtime start --project <project>
tulid runtime status
tulid runtime status --project <project>
tulid runtime stop
tulid runtime stop --project <project>
```

Work with jobs:

```bash
tulid jobs status <project>
tulid jobs run-one <project>
tulid jobs list <project>
tulid jobs show <project> <job-id>
tulid jobs logs <project> <job-id>
```

Apply or inspect transitions manually:

```bash
tulid transition <project> <task-id> <transition-id>
tulid transactions list <project>
tulid transactions recover <project>
```

Agent image and host checks:

```bash
tulid agents doctor
tulid agents build-images
tulid install docker
```

Model proxy:

```bash
tulid model-proxy serve
```

Uninstall:

```bash
tulid uninstall
```

This removes the installed app but keeps `~/.tulid/`.

## Runtime Flow

At a high level:

```text
load project workflow
  -> read tracker tasks
  -> find runnable transitions
  -> lease required resources
  -> create an execution job
  -> prepare an isolated workspace
  -> build the worker prompt packet
  -> run the configured worker
  -> receive completion evidence
  -> validate the completion in Tulid
  -> apply the transition transaction
  -> release resources and record events
```

The prompt packet is intentionally structured so implementation workers receive
a clear primary task, context priority, read-only reference material, writable
workspace rules, and a completion contract. See
[docs/runtime-prompt-architecture-plan.md](docs/runtime-prompt-architecture-plan.md)
for the design rationale behind that prompt structure.

## Repository Layout

```text
src/open_tulid/
  adapters/      tracker storage adapters
  cli/           Typer command surface
  containers/    agent Dockerfiles and image helpers
  domain/        task, transition, and validation domain models
  runtime/       jobs, scheduling, execution, resources, events, prompts
  vault/         vault/project validation
  workflow/      workflow compiler and runtime model
src/workflow_engine/
  schema and workflow loading support
tests/
  unit, runtime, workflow, adapter, and e2e coverage
```

## Testing

Run the full test suite:

```bash
uv run pytest -v
```

Useful focused runs:

```bash
uv run pytest -q tests/runtime
uv run pytest -q tests/workflow tests/domain tests/adapters
uv run pytest -q tests/e2e/test_runtime_detached_stt_workflow.py
```

Some e2e tests require Docker.

## Project Status

Open Tulid is usable but still conservative about the guarantees it claims. The
important boundaries are already in place: workflow validation, tracker
adapters, worker jobs, runtime events, resource leasing, model proxying,
completion validation, and transaction recovery.

The next hardening work is mostly around stronger end-to-end coverage, cleaner
operator ergonomics, richer validations, and continued tightening of prompt and
runtime boundaries.
