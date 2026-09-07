# Plan 3 — Consistent, executable tasks and complete breakdowns

Status: implementation plan; task generation and tracker files have not been changed by writing this document.

Parent: [reliability and polish plan](reliability-polish-plan.md). This includes the original baseline/schema work and task-quality work.

## Outcome and boundaries

Planning produces a dependency-aware set of tasks that configured `<workers>` can execute without rediscovering product decisions or negotiating incompatible interfaces. A generated task is valid at creation, loading, scheduling, rendering, and review. The complete set covers the agreed product, including integration and polish already required by its specification.

Planning is an activity assigned by the workflow. It is not a fixed worker tier or a particular tool/model. The same worker may plan and implement, and users may configure separate workers. No task schema rule depends on a worker executable or model name.

Keep one global command policy and ordinary prose acceptance criteria. Do not generate task-local command contracts, add a mandatory planning agent round, enforce tiny tasks, or expand the product scope.

## Starting evidence and code

- `templates/default_project/agents/task-breakdown.agent.md` allows free-form structure, while `vault/task_schema.py` requires description/Why/What/How/Acceptance.
- Active authoring forbids task-local command selection, but validation and existing task files still carry `accepts:` references.
- `runtime/completion.py`: `_parse_derived_task_file` and `_derived_task_plan` parse and allocate children; full batch validation must precede publication.
- `adapters/obsidian.py`, `vault/validator.py`, `runtime/task_manager.py`: tracker parsing, repair/validation, and scheduling entry points must agree.
- `runtime/context.py`, `runtime/executor.py`, `runtime/instructions.py`: planning input assembly and source lineage.
- `templates/default_project/workflow.yaml` and authoring/review instructions: existing workflow stages to improve.
- `docs/task-file-schema-plan.md` contains older task-local check guidance. Mark its conflicting sections superseded when implementation lands rather than leaving two active specifications.

## Task shape and ownership

Retain the already documented five-part body shape to minimize migration:

```markdown
---
local_id: stable-local-name
dependencies: [prerequisite-local-name]
---
# Concrete outcome

One or two sentences describing the result.

## Why
Product reasoning, settled decisions, and source references.

## What
Required behavior, inputs/outputs, interfaces, scope, and non-goals.

## How
Existing seams, prerequisite assumptions, failure behavior, and technical guidance.

## Acceptance
Observable outcomes and test expectations as prose.
The project global verification policy must pass.
```

Paths under scope are guidance about responsibility, not a file allowlist. Acceptance may name behaviors, fixtures, or tests to add, but contains no `run:`/`accepts:` selector and no independent executable command list. Workers can still run diagnostic checks while implementing; Tulid's acceptance commands come only from the global policy.

Apply this body schema to implementation tasks through workflow semantics. If the current DSL cannot declare that association for a custom task type, add only a narrow optional task-type `body_schema: implementation/v1` field and wire it through schema/compiler/domain validation. Update the default workflow explicitly. Do not infer body shape from a model name or impose it on question rounds and ideas.

Use ordinary linked references/metadata for required specification sections, canonical decisions, and prerequisites. Plan 2 owns freezing/materialization; this plan owns the source-reference representation. Prefer existing links plus unambiguous heading references before adding fields. Any added structured reference field must carry context only, not verification commands.

## Implementation sequence

### 3A. Establish one parser and validation path

Extract/reuse a pure body validator shared by derived-artifact acceptance, tracker loading/validation, repair previews, and job preparation. Validate title, description, required section presence, duplicate headings, and nonempty acceptance criteria. Treat malformed frontmatter as a structural error rather than coercing arbitrary values into dependency IDs.

Distinguish presence from meaning: the parser must not claim to prove that a requirement is well specified. Keep behavior/coverage assessment in existing planning and review instructions.

Check forbidden command fields in their defined metadata/body positions without rejecting ordinary prose such as a requirement for an application to support a `run` subcommand. Return precise diagnostics with file/section locations. Make new artifacts follow the new policy consistently; legacy compatibility belongs in explicit migration/history handling.

### 3B. Define semantic revision and source ownership

Define a semantic task revision from the normalized task requirements, dependency identities, and selected required source content identities. Preserve meaningful acceptance text; exclude board location, current workflow state, generated audit links, timestamps, and completion history.

Record the revision separately from the immutable job packet hash. Plan 1 uses the semantic revision for durable attempts. A daemon restart, different worker assignment, or logging-only edit must not renew the budget. A requirements change becomes an explicit new revision; old attempts retain their original identity and remain inspectable.

Determine canonical answer precedence from the existing question-round lineage. Preserve source references for important decisions. Do not flatten multiple answers into an unattributed summary that loses which decision superseded which.

### 3C. Strengthen existing planning inputs and instructions

Before specification/breakdown, supply repository facts and all unfinished task bodies in the relevant project scope, with IDs, states, and dependencies. Supply relevant canonical project direction and answer lineage through plan 2. Deduplicate sources; do not recursively import linked files from every unfinished task.

Have the assigned worker inspect actual manifests and integration seams, distinguish implemented behavior from remaining work, and preserve interfaces already promised by prerequisite tasks. A task should not instruct an implementation worker to select architecture already fixed by the specification.

Within the existing breakdown run, require a self-audit for:

- Each required product behavior has an implementation owner and a test expectation.
- Dependencies capture actual data/interface prerequisites rather than arbitrary board order.
- Tasks agree on names, schemas, ownership, errors, and integration boundaries.
- Existing unfinished work is reused/accounted for, without duplicate ownership.
- Final integration, failure paths, and required UI polish are represented.
- Unresolved product choices return through the existing clarification workflow before tasks are published.

Keep an inspectable coverage summary in the existing specification/authoring artifact or completion evidence. It maps requirements to task local IDs and intended verification behavior; it is not an executable check contract or an additional mandatory worker stage.

### 3D. Validate and publish the entire batch atomically

Parse every child artifact before any task, board card, parent link, or artifact is promoted. Validate unique local IDs, missing dependencies, self-dependencies, cycles, body schema, source resolution, and artifact path consistency. Allocate persisted IDs only after validation succeeds, under the existing transaction coordination.

Maintain existing same-batch dependency semantics initially. If a prerequisite is an existing unfinished task, use the existing supported task reference mechanism if one exists; otherwise the breakdown must account for that work without fabricating local IDs. A narrow explicit existing-task reference extension is warranted only if the current representation cannot express a real prerequisite, and must distinguish persisted IDs from local IDs unambiguously. Do not silently duplicate existing work to bypass the limitation.

Give rejected planning output precise batch feedback, and allow correction through the existing completion-repair protocol. Correcting one artifact must not create duplicate siblings or advance the parent early. Replayed accepted submissions must return the existing result.

### 3E. Migrate templates and active task definitions

Update authoring templates, worker procedures, parser, validator, CLI diagnostics, and documentation together. Prepare a migration preview on a tracker copy: old path/ID, proposed body, removed legacy machine fields, preserved behavioral criteria, and any decision requiring review.

Move existing prose without changing scope. Convert legacy command bullets into their underlying test expectations only when the behavior is explicit; otherwise flag the ambiguity rather than inventing it. Keep project commands in `contract.yaml`. Do not rewrite frozen job packets or treat a running task edit as an in-place change to its job.

Preserve historical tasks/artifacts for inspection. New tasks and explicitly migrated active revisions follow one policy. Active jobs continue with their original inputs or are explicitly replaced after settlement.

## Task sizing experiment

Use Wealthy Scholar task #8 as a controlled example after execution and context fixes. Its shared schemas, two language integrations, and parity verification could remain one task if the assigned worker completes it coherently. If it repeatedly stalls for a demonstrated scope reason, split into a shared foundation, each language integration, and final parity/boundary coverage with explicit dependencies.

Do not infer model limitations from infrastructure timeouts. Compare complete-context outcomes, repeated edits, test failures, and unresolved interface decisions. Avoid fixed task counts, file limits, or arbitrary duration thresholds.

## Regression and acceptance checks

- A newly generated five-part task survives acceptance, tracker reload, prompt render, and scheduler admission unchanged in meaning.
- Missing/duplicate/empty sections and malformed dependencies fail before publication with specific diagnostics.
- Custom configured task types can select the implementation schema; ideas/question rounds remain valid under their own rules.
- Task-local command selectors are rejected for new tasks; ordinary prose test expectations remain valid.
- Unknown/self/cyclic dependencies and an invalid last child leave zero partial tasks/cards/links.
- Concurrent batch submissions allocate unique IDs; replay creates no duplicates.
- Canonical answers from several rounds are retained with correct precedence in planning inputs.
- A second breakdown accounts for existing unfinished work rather than regenerating it.
- State movement and generated links do not change semantic revision; changed required behavior or source decisions do.
- Migration preserves IDs, dependency meaning, non-goals, and test behavior while removing obsolete command selection.

Extend `tests/runtime/test_completion.py`, `test_task_manager.py`, `test_context.py`, `tests/adapters/test_obsidian_adapter.py`, `tests/test_vault_validate.py`, `tests/test_project_create.py`, and workflow compiler/schema tests if the narrow declaration is needed.

## Dependencies and completion evidence

Agree reference/revision interfaces with plan 2 before coding. Parser/templates and pure batch validation can land first. Atomic publication uses the existing transaction engine and receives the recovery hardening in plan 5; final acceptance includes that integration.

Deliver a representative generated batch, its coverage summary, a migration preview, and atomicity/regression results. Completion requires an isolated Wealthy Scholar breakdown in which every original requirement is allocated, all references resolve for the eventual worker, and no manual task reformatting is needed before execution.
