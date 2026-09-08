# Task Breakdown

Break the injected implementation specification into a dependency-aware set of concrete implementation tasks.

Use the task body, injected linked context, and repository files present in the workspace as source material. Preserve the architecture defined in the implementation specification, maximize safe parallelism, and keep true prerequisites explicit.

Choose the number and size of tasks based on the actual work; there is no fixed daily structure, task count, or line count. Every generated task must still follow the shared five-part implementation body shape so it survives acceptance, tracker reload, and scheduling without reformatting. Each task must form one coherent local-model execution unit and carry the exact behavior, repository paths, interfaces, failure behavior, and product/technical reasoning relevant to it. Do not make the child choose architecture already owned by the direction or specification. Do not emit a task whose purpose is to obtain a missing product decision: the clarification loop must have settled every such decision before breakdown.

Write every task as structured prose, not a validation contract: product reasoning, technical context, canonical answers, objective, scope, requirements, non-goals, and test expectations. Verification commands are global at the project level and are inherited by every task; do not emit a per-task `run:`/`accepts:`/`accepts_if:` command list or any per-task check contract.

Emit one `ImplementationTaskFile` artifact per task under `output/`. Use the five-part implementation body shape:

```markdown
---
local_id: stable-local-name
dependencies: [other-local-name]
---
# Concrete task title

One or two sentences describing the observable outcome.

## Why
Product reasoning, settled decisions, required source references, and prerequisites.

## What
Required behavior, inputs/outputs, interfaces, scope, and non-goals.

## How
Existing seams, prerequisite assumptions, failure behavior, and technical guidance.

## Acceptance
Observable outcomes and test expectations as prose.
The project global verification policy must pass.
```

Every child must carry exactly one nonempty `## Why`, `## What`, `## How`, and `## Acceptance` section and unique `## ` headings. Do not add an execution-contract schema or any command list to the child task: Tulid applies the project global verification commands automatically after the task enters `Todo`. `dependencies` may be omitted when empty. Local IDs must be unique, and dependencies may reference only local IDs emitted in the same breakdown.

A task may describe the tests it adds as ordinary prose requirements, and those tests become part of the project test suite. Do not turn them into a per-task command contract or list of validation commands.

## Mandatory self-audit before submission

Before you submit any child task, audit the generated set against the implementation specification, the injected repository facts, and every unfinished task listed in the planning inputs. Confirm all of the following and record the result in an inspectable coverage summary included in your completion evidence:

- Every required product behavior and acceptance outcome has one implementation owner task and a test expectation.
- Every `dependencies` edge captures an actual data, interface, or prerequisite seam, never an arbitrary board order.
- Tasks agree on names, schemas, ownership, errors, and integration boundaries; no two tasks claim the same interface or deliverable.
- Existing unfinished work is reused and accounted for, without duplicate ownership or regenerating a task another artifact already owns.
- Final integration, failure paths, and required UI polish are represented somewhere in the set.
- No implementation-blocking product choice remains unresolved; any genuine unresolved choice must return through the clarification workflow instead of being published as a task.

The coverage summary must map each requirement to its owning task `local_id` and the intended verification behavior. It lives in the spec/authoring artifact or your completion evidence, and is not a per-task command contract. Keep it inspectable and concise.
