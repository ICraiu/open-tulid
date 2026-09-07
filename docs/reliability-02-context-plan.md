# Plan 2 — Complete, reproducible worker context

Status: implementation plan; no prompt/runtime changes have been made by writing this document.

Parent: [reliability and polish plan](reliability-polish-plan.md). This covers context handoffs between workflow tasks assigned to `<workers>`.

## Outcome and boundaries

A worker receives the full assigned task and can read every settled decision, interface, and prerequisite result it needs inside its workspace. The packet used for a real job is reproducible from frozen inputs and remains inspectable after the vault changes.

The workflow assigns direction, clarification, specification, breakdown, implementation, and review to configured `<workers>`. One worker may serve several responsibilities, or different workers may serve each. The prompt compiler follows task/transition semantics, not provider names or assumed worker tiers.

Keep global commands separate from task requirements. Do not restore generated per-task execution contracts, predicted file allowlists, a retrieval service, or a new context-authoring agent stage.

## Starting evidence and code

- `runtime/execution_contracts.py`: `compile_standard_execution_contract` freezes no context excerpts and embeds the task body as a generated objective.
- `runtime/executor.py`: the legacy prompt route resolves parent/linked context separately from the frozen implementation route.
- `runtime/context.py`: existing linked document traversal, canonical question-round answers, containment checks, and deduplication.
- `runtime/instructions.py`, `runtime/prompts.py`, `runtime/prompt_versions.py`: instruction resolution, structured sections, budgets, prompt manifests, review evidence.
- `runtime/task_manager.py`, `runtime/workspaces.py`: job creation and materialization of saved execution inputs.
- `cli/main.py`: prompt preview, explain, lint, and historical inspection.

The saved Wealthy Scholar task #8 packet asks for enums/rules from the implementation specification but includes no selected excerpts. It duplicates the task between Mission and Execution Contract. Truncating the first copy is visible; losing the specification is the substantive defect.

## Decisions and shared interface

1. Introduce one internal frozen context bundle used by both legacy planning and global-policy implementation paths. Extend the existing records rather than adding a separate public configuration language.
2. Each source entry records its logical role, original reference, content digest, byte count, required/optional status, workspace path, and inclusion reason. Full frozen bytes must exist, not just references to a mutable vault.
3. Keep task semantics and reference selections defined in [plan 3](reliability-03-task-quality-plan.md). Selection metadata is task context, never a command contract or a writable-path restriction.
4. A budget may shorten optional background or replace a full reference in the prompt with an explicit readable file reference. It may not silently truncate required task behavior. Required files must be present and named in the prompt's reading instructions.
5. Compiler output depends on frozen task/context, workflow, instructions, command policy, and compiler version. Provider/model names do not change the meaning of those inputs; configured context capacity may affect optional packaging.

## Implementation sequence

### 2A. Trace and unify input resolution

Map the two current prompt routes and identify ownership of task text, parent lineage, canonical answers, linked specifications, instructions, repository facts, and completion syntax. Remove duplicate assembly by making both paths consume the same resolver output at job creation.

For planning tasks, include the current source idea/specification, its canonical answer lineage, relevant existing project direction, repository inventory, and unfinished task bodies supplied by plan 3. Do not recursively pull every unfinished task's linked documents into the packet.

For implementation tasks, include the full task, specifically relevant specification/answer references, and accepted prerequisite interface/result evidence. Do not substitute broad product summaries for precise rules needed by the task. Existing code is available for inspection; repository facts should point to manifests/seams rather than repeat a directory dump.

### 2B. Freeze source bytes with provenance

Resolve references under the configured project roots with the existing path containment rules. Detect missing/ambiguous links, parent cycles, conflicting canonical answer records, and invalid heading selections before a job is admitted.

Use plan 3's selected references where present. For legacy tasks, follow their source lineage to the canonical specification and answers rather than accepting an empty context silently. If precise section selection is unavailable, include the full required document as a workspace reference and surface its cost; do not fabricate a summary.

Store frozen content with the job's durable inputs and materialize it under the existing internal workspace area, for example `.open-tulid/context/`. Deduplicate identical content while retaining all logical provenance references. Exclude these internal files from application promotion.

Required input resolution must fail before worker launch, with a diagnostic naming the task and missing/conflicting source. Copy/write failures must leave no apparently runnable job with an incomplete bundle.

### 2C. Render one coherent prompt

Use a stable section order:

1. Assigned outcome and complete task requirements.
2. Required reading with exact workspace paths and why each file matters.
3. Prerequisite interfaces/results and relevant repository facts.
4. Applicable worker procedure and global verification commands.
5. Completion submission and repair protocol.

Keep the task body in one authoritative section. Remove the synthetic repetition of task text under an execution-contract objective from new prompts; preserve historical deserialization. The global policy section governs verification commands, not product decisions.

Render settled answer precedence explicitly: later answers override earlier statements only where the source lineage establishes that relationship. If the specification contradicts an answer and precedence cannot resolve it, report a preparation/planning blocker rather than arbitrarily favoring the longest/newest document.

### 2D. Make budget handling explicit

Separate inline prompt capacity from workspace reference capacity. Reserve room for mandatory procedure/completion syntax and the full task. Fill remaining inline space with relevant excerpts, then optional background. Required reference files remain complete even when only their reading instructions fit inline.

Preserve the current character-budget accounting where needed for compatibility, but do not label characters as tokens. If a configured provider exposes usable token limits, record the estimator and headroom; do not claim exact accounting otherwise. An oversized task or mandatory instruction block produces a named budget error instead of truncated behavior.

Lint must catch duplicate authoritative task content, unresolved reading paths, missing required sources, forbidden task-local command blocks, malformed completion examples, and discrepancies between manifest hashes and saved bytes. Optional omissions must appear in `prompts explain` with reasons.

### 2E. Align preview, repair, and review

Have preview call the same resolver/compiler with a synthetic job identity and no scheduler mutation. Saved-job inspection reads the historical bundle and never reconstructs it from the current vault. Normalize only ephemeral completion fields when comparing preview with a real packet.

Repair receives the same frozen requirements and applicable evidence plus new verifier feedback. A reduced retry packet can remove repetition and optional context, but cannot drop required source files. If the requirement set itself must change, create an explicit new task revision rather than silently repairing the old packet.

Review receives the task/context bundle plus the verified change set, check results, and repair history from plans 4–5. If evidence is unavailable, review is blocked or clearly historical/unverified; it must not receive an empty diff represented as a completed implementation.

## Tests and acceptance

Extend `tests/runtime/test_context.py`, `test_instructions.py`, `test_execution_contracts.py`, `test_workspaces.py`, `test_executor.py`, and prompt CLI coverage in `tests/test_operator_cli.py`.

| Fixture | Required result |
| --- | --- |
| Wealthy Scholar task #8 with specification and answer lineage | The task's required enum, serialization, and parity rules are readable from named workspace files without host-vault access. |
| Required section at the end of a large document | Full required text remains in the bundle; prompt points to it; no silent tail truncation. |
| Full task alone exceeds its supported budget | Preparation fails before execution with a precise diagnostic. |
| Duplicate links/identical documents | Content stored once, provenance retained, task not repeated. |
| Missing link, cycle, ambiguous heading, conflicting answer | Preparation fails deterministically before worker launch. |
| Vault edit after scheduling | Saved packet and reference bytes remain unchanged; next task revision sees the edit. |
| Preview versus scheduled job from identical inputs | Same substantive packet and bundle identities after normalizing ephemeral fields. |
| Repair after failed test | Requirements unchanged; specific feedback added; optional context reduction is visible. |
| Review after accepted implementation | Actual candidate/change evidence is supplied, including no-change review support. |

Use sanitized copies of real task/specification material as fixtures. Do not copy credentials or private runtime configuration into the repository.

## Dependencies, migration, and completion

Agree the task reference/semantic revision interface with plan 3 first. The context resolver and legacy fallback can then be implemented independently. Final review integration consumes the evidence records from plans 4–5; use explicit fixture records until those exist.

Bump prompt/input serialization versions where the representation changes. Read old packets as historical records; do not reinterpret them as complete new bundles. Apply new compilation only to newly admitted jobs or explicit new task revisions. Migrate active task references on an isolated tracker copy before the live project.

Deliver source changes, representative prompt/bundle artifacts, budget-boundary tests, and a before/after rendering of task #8. Completion requires the assigned worker to explain and implement the assigned rules using only its supplied workspace context, with no operator supplying missing specification text during the run.
