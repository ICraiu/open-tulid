# Writing a good `workflow.yaml`

Each Tulid project owns a `workflow.yaml`. This file defines:

1. the **vocabulary** of the project — states, task types, and artifact types
2. the **runtime tools** it may use — workers, validations, and operations
3. the **motion** through the system — transitions from one state to another

A useful workflow is not merely valid YAML. It should make the work legible: what kinds of tasks exist, what evidence is required before work advances, and which transitions Tulid may schedule automatically.

## A small complete example

```yaml
schema_version: 1

storage:
  obsidian:
    boards:
      Work: kanban/Work.md
    state_mappings:
      - state: Todo
        board: Work
        column: Todo
      - state: CodeReview
        board: Work
        column: Code review

statements:
  - kind: state
    id: Todo

  - kind: state
    id: CodeReview

  - kind: task_type
    id: CodingTask
    requirements:
      CodeReview:
        artifacts: [ImplementationSummary, TestResult]

  - kind: artifact_type
    id: ImplementationSummary
    template: implementation-summary.md

  - kind: artifact_type
    id: TestResult
    template: test-result.md

  - kind: validation_type
    id: tests_pass
    args:
      command:
        type: string

  - kind: worker
    id: codex
    type: codex
    instructions: [default]

  - kind: transition
    id: ImplementTask
    task_type: CodingTask
    from: Todo
    to: CodeReview
    worker: codex
    default_for_scheduler: true
    requires:
      artifacts: [ImplementationSummary, TestResult]
      validations:
        - type: tests_pass
          args:
            command: "python -m pytest"
      changed_files:
        required: true
```

Order is flexible: references may point to statements declared later. In practice, humans read workflows more easily when declarations come first and transitions come last.

## Top-level structure

```yaml
schema_version: 1
storage:        # optional
statements:     # required
```

### `schema_version`

Required. Today the only supported value is `1`.

### `storage`

Optional. This maps workflow states onto the external tracker. The implemented adapter is Obsidian:

```yaml
storage:
  obsidian:
    boards:
      Work: kanban/Work.md
    state_mappings:
      - state: Todo
        board: Work
        column: Todo
```

Each `state_mappings` entry must reference:

- an existing workflow state
- a named board declared in `boards`
- a unique board/column pair

Do not map the same state twice.

### `statements`

Required list of declarations. Supported kinds:

```text
state
task_type
artifact_type
validation_type
worker
operation_type
transition
```

## The statement kinds

### `state`

Declares a lifecycle state.

```yaml
- kind: state
  id: Todo
```

Use states to describe meaningful checkpoints, not every tiny activity. `Todo`, `InProgress`, `CodeReview`, and `Done` are usually clearer than a dozen near-duplicates.

### `task_type`

Declares a family of work and, optionally, the requirements a task must satisfy when it is persisted in a given state.

```yaml
- kind: task_type
  id: CodingTask
  instructions: [backend-python]
  requirements:
    CodeReview:
      artifacts: [ImplementationSummary]
```

`requirements` is keyed by **state id**. These are state invariants: if a `CodingTask` is already in `CodeReview`, Tulid expects its task record to show the required artifact links.

`instructions` may be one ref or a list of refs into the project's `agents/` directory. `backend-python` resolves to `agents/backend-python.agent.md` when present.

### `artifact_type`

Names a kind of produced evidence.

```yaml
- kind: artifact_type
  id: ImplementationSummary
  template: implementation-summary.md
```

`template` is an opaque reference carried by the workflow definition. Use artifacts for things worth preserving or reviewing: implementation summaries, test results, rollout notes, screenshots, and so on.

### `validation_type`

Declares a validation that may later be called from a requirement set.

```yaml
- kind: validation_type
  id: tests_pass
  args:
    command:
      type: string
```

The `id` must match a registered validation implementation. In the current runtime, that means one of the built-ins listed below.

Argument definitions support:

```yaml
type: string | integer | boolean | state_ref | task_type_ref | artifact_ref | validation_ref | worker_ref | operation_ref
required: true | false
many: true | false
```

Use reference types when the argument should point at another workflow declaration; Tulid will validate the reference for you.

### `worker`

Declares an execution worker.

```yaml
- kind: worker
  id: codex
  type: codex
  instructions: [default]
```

If `type` is omitted, Tulid uses the `id` as the implementation id. Built-in worker implementations are:

```text
local_llm
shell_command
human_approval
noop
codex
opencode
```

A transition with a worker is scheduler-eligible; a transition without one is a manual/runtime transition only.

### `operation_type`

Declares an operation that may be used inside a transition transaction.

```yaml
- kind: operation_type
  id: move_task
  args:
    to:
      type: state_ref
      required: true
```

As with validations, `id` must match a registered implementation.

### `transition`

Moves one `task_type` from one state to another.

```yaml
- kind: transition
  id: ImplementTask
  task_type: CodingTask
  from: Todo
  to: CodeReview
  worker: codex
  default_for_scheduler: true
  instructions: [implementation]
  requires:
    artifacts: [ImplementationSummary]
    validations:
      - type: tests_pass
        args:
          command: "python -m pytest"
    changed_files:
      required: true
```

Fields:

- `task_type`, `from`, and `to` are required.
- `worker` is optional.
- `default_for_scheduler: true` is useful when multiple worker-backed transitions leave the same state. If there is more than one scheduler-eligible choice, exactly one should be the default.
- `instructions` add transition-specific agent guidance.
- `requires` describes acceptance requirements for the transition.
- `transaction` optionally lists side-effecting operations to run.
- `derives` optionally creates new child tasks from submitted task-file artifacts while the parent still moves to its own `to` state.

### Deriving child tasks

Use `derives` when a transition decomposes one parent into many new tasks of the same shape:

```yaml
- kind: transition
  id: BreakSpecIntoComponents
  task_type: Spec
  from: Ready
  to: Done
  worker: codex
  derives:
    task_type: Component
    state: Todo
    artifact_type: ComponentTask
```

The parent still follows the normal transition (`Ready -> Done`). In addition, Tulid expects one or more submitted artifacts of `ComponentTask`, creates one child task per artifact in `Todo`, sets `parent_id`, resolves dependencies between the emitted children, and adds a canonical `## Derived tasks` section to the parent.

Each child-task artifact is a Markdown file with YAML frontmatter:

```md
---
local_id: parser
dependencies: []
---
# Build parser

Implement the parser boundary.
```

Dependencies refer to sibling `local_id` values inside the same completion batch:

```md
---
local_id: cli
dependencies: [parser]
---
# Wire parser into CLI
```

Tulid, not the worker, assigns real task IDs and writes the final child task files. This keeps the model useful for decomposition while the trusted runtime owns identity, links, and state placement.

## Requirement sets

Requirement sets appear in two places:

1. under `task_type.requirements.<state>` — what must be true for persisted tasks in that state
2. under `transition.requires` — what a completion must provide before the transition is accepted

Shape:

```yaml
requires:
  artifacts:
    - ImplementationSummary
  validations:
    - type: tests_pass
      args:
        command: "python -m pytest"
  changed_files:
    required: true
```

Supported keys:

- `artifacts`: artifact type ids
- `validations`: validation calls
- `changed_files.required`: whether the completion must report changed files

The distinction matters:

- **state requirements** protect the shape of the project after the fact
- **transition requirements** gate acceptance at execution time

For important evidence, use both. The transition prevents a weak completion from entering the state; the task-type requirement makes the state self-checking later.

## Transactions

Transitions may include a transaction:

```yaml
- kind: transition
  id: Promote
  task_type: CodingTask
  from: CodeReview
  to: Done
  transaction:
    steps:
      - op: git_add
        args:
          paths: ["."]
      - op: git_commit
        args:
          message: "Complete task"
      - op: move_task
        args:
          to: Done
```

Every `op` must refer to a declared `operation_type`. Use transactions for explicit side effects, not for describing the business logic of the state machine itself.

## Built-in validations

Declare these as `validation_type` statements when you use them in requirements.

| id | Purpose | Common args |
| --- | --- | --- |
| `project_build` | runs a build/test command | `command` |
| `git_status_clean` | requires a clean Git worktree | none |
| `file_exists` | checks a project-relative file | `path` |
| `artifact_in_vault` | checks artifact exists under vault/project root | `path` or `artifact` |
| `artifact_link_in_vault` | checks a resolved artifact link remains in vault | `link` |
| `artifact_matches_template` | checks required Markdown sections | `path` or `content`, `sections` |
| `template_sections_present` | checks headings exist | `path` or `content`, `sections` |
| `template_required_fields_present` | checks named fields have values | `path` or `content`, `fields` |
| `artifact_has_required_text` | checks non-empty content or substring | `path` or `content`, `text` |
| `branch_exists` | checks a Git branch exists | `branch` |
| `tests_pass` | runs a test command | `command` |
| `link_target_exists` | resolves a link and checks the target exists | `link` |

## Built-in operations

Declare these as `operation_type` statements when you use them in transactions.

| id | Purpose | Common args |
| --- | --- | --- |
| `move_task` | moves a task to another state | `to` |
| `copy_file` | copies a file | `source`, `target` |
| `copy_field` | copies a field between mappings | `source`, `target`, `source_field`, `target_field` |
| `set_field` | writes a field on a mapping | `target`, `field`, `value` |
| `link_artifact` | adds an artifact link to the task | `artifact` |
| `git_add` | stages paths | `paths` |
| `git_commit` | commits staged changes | `message` |
| `git_reset_hard` | hard resets Git state | `target`; destructive and requires approval |
| `create_branch` | creates a Git branch | `branch` |
| `checkout_branch` | checks out a branch | `branch`, optional `create` |
| `write_file` | writes text to a file | `path`, `content` |
| `append_event` | appends an event payload | `event` or event fields |
| `update_kanban_view` | updates task position like `move_task` | `to` |

## Instructions

`instructions` may appear on:

- `task_type`
- `worker`
- `transition`

Tulid resolves refs from the project's `agents/` directory. For a scheduled transition, the prompt packet is assembled from worker instructions, then task-type instructions, then transition instructions. This is a good layering model:

- worker instructions: enduring behavior for the tool
- task-type instructions: standards for a category of work
- transition instructions: what matters in this specific phase

Tulid also injects linked project context into the same prompt packet:

- every path listed in the task's `artifact_links`
- every `[[wiki link]]` found in the task body
- every further `[[wiki link]]` found inside those linked files

Linked files are resolved inside the project only, deduplicated, cycle-safe, and size-limited before they are handed to the sandboxed worker. This lets a later stage consume earlier artifacts without granting the worker direct access to the tracker vault.

## A practical authoring recipe

When designing a workflow, this order tends to stay sane:

```text
1. Name the states.
2. Name the task types.
3. Decide what evidence each important state should imply.
4. Name artifact types for that evidence.
5. Add only the validations that prove meaningful claims.
6. Add workers.
7. Add transitions.
8. Mark a scheduler default only where ambiguity exists.
9. Add transactions last, once the state model itself is already clear.
```

Good workflows are usually a little stricter than the happy path. If `CodeReview` means “someone can review this safely,” require the artifacts and validations that make that sentence true.

## Common mistakes

- **Using a validation without declaring its `validation_type`.**
- **Using an operation in a transaction without declaring its `operation_type`.**
- **Referencing undeclared states, workers, artifacts, or task types.**
- **Giving several scheduler-eligible transitions from one state with no single default.**
- **Packing too much meaning into state names instead of artifacts and validations.**
- **Treating transition requirements and task-state requirements as interchangeable.** They serve different moments in the lifecycle.

## Minimal valid file

```yaml
schema_version: 1
statements:
  - kind: state
    id: Todo
```

That file is valid, but not yet very useful. A good workflow earns its complexity by making project truth easier to inspect.
