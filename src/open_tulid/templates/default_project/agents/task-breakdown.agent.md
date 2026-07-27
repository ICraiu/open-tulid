# Task Breakdown

Break the injected implementation specification into a dependency-aware set of concrete implementation tasks.

Use the task body, injected linked context, and repository files present in the workspace as source material. Preserve the architecture defined in the implementation specification, maximize safe parallelism, and keep true prerequisites explicit.

Choose the number, size, and Markdown structure of tasks based on the actual work; there is no fixed daily structure, task count, line count, or mandatory section list. Each task must still form one coherent local-model execution unit and carry the exact behavior, repository paths, interfaces, failure behavior, and acceptance checks relevant to it. Do not make the child choose architecture already owned by the direction or specification.

Emit one `ImplementationTaskFile` artifact per task under `output/`. The only storage-required shape is:

```markdown
---
local_id: stable-local-name
dependencies: [other-local-name]
---
# Concrete task title

The freely structured task body.
```

`dependencies` may be omitted when empty. Local IDs must be unique, and dependencies may reference only local IDs emitted in the same breakdown. Do not add an execution-contract schema to the child task: Tulid prepares that internal contract automatically after the free-form task enters `Todo`.
