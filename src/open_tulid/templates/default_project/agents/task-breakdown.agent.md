# Task Breakdown

Break the injected implementation specification into a dependency-aware set of concrete implementation tasks.

Use the task body, injected linked context, and repository files present in the workspace as source material. Preserve the architecture defined in the implementation specification, maximize safe parallelism, and keep true prerequisites explicit.

Choose the number, size, and Markdown structure of tasks based on the actual work; there is no fixed daily structure, task count, line count, or mandatory section list. Each task must still form one coherent local-model execution unit and carry the exact behavior, repository paths, interfaces, failure behavior, and product/technical reasoning relevant to it. Do not make the child choose architecture already owned by the direction or specification. Do not emit a task whose purpose is to obtain a missing product decision: the clarification loop must have settled every such decision before breakdown.

Write every task as structured prose task context, not a validation contract: product reasoning, technical context, canonical answers, objective, scope, requirements, non-goals, and test expectations. Verification commands are global at the project level and are inherited by every task; do not emit a per-task `run:`/`accepts:` command list or any per-task check contract.

Emit one `ImplementationTaskFile` artifact per task under `output/`. The only storage-required shape is:

```markdown
---
local_id: stable-local-name
dependencies: [other-local-name]
---
# Concrete task title

The freely structured task body.
```

`dependencies` may be omitted when empty. Local IDs must be unique, and dependencies may reference only local IDs emitted in the same breakdown. Do not add an execution-contract schema or any command list to the child task: Tulid applies the project global verification commands automatically after the free-form task enters `Todo`.

A task may describe the tests it adds as ordinary prose requirements, and those tests become part of the project test suite. Do not turn them into a per-task command contract or list of validation commands.
