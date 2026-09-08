# Migration preview — plan 3, step 3E

This is the tracked preview for migrating templates and active task definitions
to one prose-only task policy. It is a planning/migration artifact, not a
run-time contract. Frozen job packets and historical tasks are not rewritten;
only new tasks and explicitly migrated active revisions follow this policy.

Context: `docs/reliability-03-task-quality-plan.md` step 3E. Active jobs keep
their original inputs and are only replaced after settlement.

## One policy for new and migrated active tasks

Every implementation task body uses the shared five-part shape:

```markdown
---
local_id: stable-local-name
dependencies: [prerequisite-local-name]
---
# Concrete outcome

One or two sentences describing the observable outcome.

## Why
Product reasoning, settled decisions, required source references, prerequisites.

## What
Required behavior, inputs/outputs, interfaces, scope, and non-goals.

## How
Existing seams, prerequisite assumptions, failure behavior, and technical guidance.

## Acceptance
Observable outcomes and test expectations as prose.
The project global verification policy must pass.
```

- `local_id` / `dependencies` stay as structured YAML metadata; no legacy
  machine fields are preserved there.
- `## Acceptance` is prose-only: no `accepts:`, `accepts_if:`, or `run:`
  selector, and no independent per-task command list. Project verification
  commands live only in `contract.yaml`.

## Template migration

### `agents/task-breakdown.agent.md`

| Old | New |
| --- | --- |
| "no fixed ... mandatory section list" / "The freely structured task body" | Fixed five-part body shape required for every child task |
| `# Concrete task title` + unconstrained body | `# title`, description, `## Why`, `## What`, `## How`, `## Acceptance` |
| "free-form task enters `Todo`" | "task enters `Todo`" (no free-form wording) |
| no per-task `run:`/`accepts:` list (already present) | unchanged, extends to `accepts_if:` |

### Worker procedures

`agents/peon-implementation.agent.md`:

| Old | New |
| --- | --- |
| "Preserve the execution contract as the scope boundary" | "Preserve the task body as the scope boundary; verification commands come only from the project global contract (`contract.yaml`)" |
| "Read the current task and treat its execution contract as authoritative" | "Read the current task body and the project global contract; treat the task body and its `## Acceptance` as authoritative" |

`agents/self-review.agent.md`:

| Old | New |
| --- | --- |
| "Re-read the task and its execution contract when present" | "Re-read the task body ... verification commands come only from the project global contract (`contract.yaml`)" |

`agents/implementation-spec.agent.md`:

| Old | New |
| --- | --- |
| "...needed by task breakdown and execution-contract authoring" | "...needed by task breakdown" |

`agents/direction-authoring.agent.md`:

| Old | New |
| --- | --- |
| "later execution contracts must not invent" | "later implementation tasks must not invent" |

`agents/implementation-spec-template.agent.md`:

| Old | New |
| --- | --- |
| (implementation-spec only) | Note added: generated tasks use the five-part body schema, prose-only acceptance, no per-task command selectors |

## Removed legacy machine fields

The legacy task-file-schema machine block is removed from the validator and
from migrated tasks:

```yaml
## Acceptance (before)
accepts: [<declared profile or check id>]
accepts_if:
  - <machine criterion>
run: [<extra deterministic command>]

## Acceptance (after migration)
- <the behavioral outcome, kept verbatim as prose>
```

- `task.acceptance_run_forbidden` now fires for `accepts:`, `accepts_if:`, and
  `run:`.
- `task.acceptance_run_unknown` and declared-id resolution are removed; the
  `declared_ids_for_project` helper is no longer needed for schema validation.
- `## Acceptance` must still have at least one prose criterion
  (`task.acceptance_criteria_missing`).

## Preserved behavioral criteria and decisions requiring review

Migration moves existing prose without changing scope. When a legacy command
bullet's underlying test expectation is explicit (e.g. "the /healthz endpoint
is added"), keep it verbatim as prose. When the intended behavior is implied
but not stated (e.g. a bare `pytest` or `npm run build` bullet), flag the
ambiguity for review instead of inventing the requirement. Project commands
already live in `contract.yaml` and are inherited, so no bullet is needed to
make them run.

## Files changed for this step

- `src/open_tulid/templates/default_project/agents/*.agent.md` (authoring and
  worker procedures)
- `src/open_tulid/vault/task_schema.py` (parser/validator strictness)
- `src/open_tulid/adapters/obsidian.py` (drop declared-id resolution)
- `docs/task-file-schema-plan.md` (mark conflicting sections superseded)
