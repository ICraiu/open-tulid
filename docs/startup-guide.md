# Startup guide

Tulid coordinates work between a tracker and coding agents. Today the implemented tracker adapter is Obsidian; the configuration shape leaves room for a future text tracker without making project layout Obsidian-specific.

## 1. Install and initialize

```bash
pip install -e .
tulid init
```

`tulid init` creates one user-owned config file:

```text
~/.tulid/config.yaml
```

Tulid does not search the current directory for config files. Machine-wide runtime state also lives under `~/.tulid/`; project-owned work stays in the tracker project.

## 2. Configure Tulid

Edit `~/.tulid/config.yaml`:

```yaml
tracker:
  type: obsidian
  root: /home/me/obsidian

projects:
  Agent:
    path: Agent
    repo_root: /home/me/repo/Agent
    main_branch: main

runtime:
  docker_executable: docker
  shared_workspace_root: workspaces
  container_workspace: /workspace/project
  image_tag_prefix: open-tulid/agent
  default_timeout_seconds: 3600
  worker_images: {}
  worker_args: {}
  worker_resources: {}
  worker_types: {}
  env: {}
```

`tracker.root` is the root directory of the tracker. Each key under `projects` is a Tulid project; `path` is relative to `tracker.root`, while `repo_root` points at the code repository agents should work on. Relative runtime paths are resolved from `~/.tulid/`, so the default workspace root becomes `~/.tulid/workspaces`.

Resource and model-proxy settings belong in this same file when needed:

```yaml
resources:
  remote-llm:
    kind: model
    capacity: 1
    proxy: openai

model_proxy:
  openai:
    kind: openai
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
```

## 3. Create a project

First add the project to `projects:` in config, then run:

```bash
tulid project Agent
```

Tulid creates the project under the tracker root:

```text
<tracker-root>/Agent/
  workflow.yaml
  agents/
    default.agent.md
  kanban/
  tasks/
  docs/
  events/
```

Each project owns its own `workflow.yaml`, because different projects may need different processes. `agents/` is where project instruction files live. Tulid keeps accepted artifacts in `<project>/artifacts/<task-id>/...` and human/machine event logs in `<project>/events/`.

## 4. Define the workflow and instructions

The scaffolded workflow is intentionally small: one `Todo` state and a default `task` type. Extend it with states, workers, transitions, validations, and storage mappings. Instruction references are declared in the DSL, for example:

```yaml
statements:
  - kind: worker
    id: codex
    instructions: [default]

  - kind: transition
    id: ImplementTask
    from: Todo
    to: CodeReview
    worker: codex
    instructions: [implementation]
```

References resolve inside the project `agents/` directory. `default` resolves to `agents/default.agent.md`; `implementation` resolves to `agents/implementation.agent.md`. Tulid validates references while loading the workflow and refuses missing or escaping paths. When a worker starts, Tulid builds its prompt packet from the applicable task, worker, and transition instructions together with the rest of the task context.

## 5. Add tasks and boards

For Obsidian, place task notes in `tasks/` and board files in `kanban/` according to the workflow storage mapping. A card may link to a human filename such as `[[Add health endpoint]]`. If Tulid sees a task note without an `id`, it generates one once and injects frontmatter into that note. It does not rename the note and does not rewrite Kanban links.

## 6. Validate and run

```bash
tulid validate
tulid vault validate
tulid tasks list Agent
tulid tasks runnable Agent
tulid jobs schedule Agent
tulid jobs run-one Agent
```

A normal execution moves through this spine:

```text
tracker task -> workflow validation -> scheduled job -> isolated workspace
             -> worker command -> trusted completion -> events/artifacts/state move
```

Transient machinery is global:

```text
~/.tulid/jobs/<project>/<job-id>/
~/.tulid/workspaces/<job-id>/
~/.tulid/runtime/<project>.json
~/.tulid/model-proxy-runtime.json
```

Inside a workspace, Tulid writes worker-local control files under `.open-tulid/` such as `prompt-packet.md`, `job-context.json`, and execution logs. Those are disposable workspace internals, not project configuration.

For long-running operation:

```bash
tulid runtime start Agent
tulid runtime status Agent
tulid runtime stop Agent
```

## 7. First-run checklist

```text
[ ] ~/.tulid/config.yaml exists
[ ] tracker.root exists
[ ] project is listed under projects:
[ ] project directory contains workflow.yaml and agents/
[ ] workflow instruction refs resolve to files in agents/
[ ] repo_root exists if workers need code checkout
[ ] tulid validate passes
[ ] tulid vault validate passes
[ ] Docker is usable if running container workers
```

If validation fails, treat the first diagnostic as the system telling you which invariant is missing; Tulid is deliberately strict before it lets agents touch work.
