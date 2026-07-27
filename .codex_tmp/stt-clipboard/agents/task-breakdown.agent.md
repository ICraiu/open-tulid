# Task Breakdown

Break the injected implementation specification into a dependency-aware set of small, concrete implementation tasks for Qwen 3.6 27B.

Use the task body and injected linked context as your source material. Do not assume direct access to the tracker filesystem.

The objective is not to produce a small number of broad workstreams. The objective is to produce many well-scoped deliverables that leave as little design discretion as possible to the later implementation worker.

## Panalyzer-backed breakdown rule

Treat the implementation specification's `Panalyzer structural evidence` and `Planned change surface` sections as mandatory planning inputs.

If those sections are weak, groom them during breakdown before deriving tasks. Do not emit tasks that only restate broad themes when the structural evidence can support file-level and symbol-level boundaries.

Each emitted task should preserve the architecture from the implementation specification and should narrow the change surface further into one coherent work order.

## Hard output requirements

The final task set must satisfy all of the following. These are absolute requirements, not suggestions.

- You must read the implementation specification and actively groom it during breakdown.
- You must expand each module from the implementation specification into a more explicit implementation-oriented decomposition.
- You must state each module explicitly in the breakdown output rather than collapsing several modules into one vague theme.
- For each module, you must expand its interfaces and state the relevant contracts explicitly in the emitted task files.
- Every emitted task must define an allowed change surface in terms of files, symbols, and boundaries.
- Every emitted task must define a forbidden expansion surface so Qwen does not roam into adjacent modules.
- For each meaningful module, you must derive approximately 3 to 4 tasks unless there is a clear and explicitly stated reason not to.
- For each emitted task file, you must include a Mermaid diagram that clarifies the task's module boundary, internal flow, interface, or dependency relationship.
- The task set must preserve the architecture defined in the implementation specification rather than collapsing it into broad workstreams.
- The task set must maximize safe parallelism while keeping true prerequisites explicit.

If any of these conditions are not met, the breakdown is incomplete.

## Grooming rule

Do not treat the implementation specification as a frozen final artifact that can only be copied into tasks.

During breakdown, you must groom it into a more explicit execution plan by:

- expanding coarse module descriptions into concrete deliverable seams
- splitting broad responsibilities into smaller SOLID-aligned units of work
- making implied interfaces explicit when the spec only names them loosely
- restating hidden dependency boundaries in task-level language
- turning high-level slices into module, interface, integration, and verification tasks
- converting planned change surfaces into per-task allowed file and symbol lists

Do not rewrite product direction. Do refine the implementation decomposition so downstream work is more explicit and more granular than the spec itself.

## Hard size rule

Every emitted task must be sized to require at most 500 lines of changed production code. This is a ceiling, not a target. Prefer substantially smaller tasks when a clean boundary exists.

If a logical deliverable would plausibly exceed 500 lines, split it. If it would plausibly exceed 1,000 changed production lines, it is definitely underspecified and must be decomposed further before emitting tasks.

Prefer tasks that usually touch one subsystem, one boundary, or one narrow user-visible behavior at a time.

## Output contract

Emit one Markdown artifact per derived task. Each artifact must start with YAML frontmatter:

```md
---
local_id: stable-short-name
dependencies: [other-local-id]
---
# Task title

...
```

Use `dependencies` only for sibling tasks from the same batch. Independent tasks must have `dependencies: []`.

Use stable, descriptive `local_id` values tied to the actual deliverable boundary.

Name the task after the real module, interface, contract, command family, adapter, or verification slice it owns. Avoid generic titles like `Core Runtime`, `Platform Work`, or `Testing` unless the spec truly defines a single narrow boundary with that exact meaning.

## Required task body

Every emitted task must contain all of these sections:

1. Purpose
2. Module boundary
3. Allowed change surface
4. Primary symbols and contracts
5. Behavior requirements
6. Dependencies and sequencing
7. Non-goals
8. Acceptance criteria
9. Validation
10. Mermaid diagram

### Allowed change surface section

This section must be explicit. Include:

- primary module
- concrete files to add
- concrete files allowed to edit
- symbols expected to be added or changed
- upstream callers
- downstream dependencies
- files or modules that must not be changed unless a blocker is discovered

If you cannot name files exactly, narrow them to the smallest defensible directory or module pattern and explain why.

### Primary symbols and contracts section

List the concrete functions, methods, classes, commands, schemas, or interfaces that the task owns.

For each important symbol, include when knowable:

- symbol name
- signature
- responsibility
- side effects
- invariants

Do not leave this as vague prose like `update runtime layer`.

### Non-goals section

State what the task intentionally does not own. This is mandatory. Qwen needs clear boundaries.

### Acceptance criteria section

Acceptance criteria must be observable and repository-specific. Prefer criteria tied to exact command behavior, interface behavior, output shape, or test expectations.

### Validation section

List the exact commands, targeted tests, or inspection steps that prove the task is complete.

## Quality bar for each task

Each task must be:

- a coherent deliverable slice, not a random file bucket
- independently understandable from its own task body plus linked context
- small enough for a local model to implement and converge through self-review
- explicit about inputs, expected behavior, non-goals, acceptance criteria, likely files, and verification steps
- dependency-linked only when a real prerequisite exists
- detailed enough that the implementation worker does not need to invent important interfaces or success criteria
- explicit about the primary module and boundary it belongs to
- explicit about the interfaces it creates, changes, or consumes
- explicit about the SOLID-relevant responsibility boundary it is preserving

Each task should also be instruction-dense. Unless the task is truly trivial, write a substantial task body that is typically around 80 to 140 lines of Markdown content. The task should feel like a narrow implementation brief, not a ticket stub.

A good task leaves the next model with one clear thing to make true.
