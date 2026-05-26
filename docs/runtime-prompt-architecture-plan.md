# Runtime Prompt Architecture Plan

## Goal

Define a Tulid-level prompt architecture that makes execution transitions, especially `ImplementTask`, behave as narrowly scoped implementation work rather than broad planning work.

This plan is intentionally scoped to prompt construction and prompt-visible context organization only.

It does **not** include:

- compact parent or spec summaries
- output-path enforcement
- forbidden-file validation
- context extraction or semantic ranking outside the prompt itself

## Why This Change Matters

Tulid currently builds a valid prompt packet, but the effective hierarchy inside that packet is too weak for implementation-style work.

The main symptoms are:

- the worker receives a small implementation task
- the worker also receives a large amount of broader project and planning context
- the prompt does not make the authority ordering strong enough
- the worker drifts into project-wide planning behavior
- the worker treats `output/` as a plausible place to rewrite planning artifacts

The issue is not raw context-window capacity. The issue is prompt structure, prompt salience, and insufficiently explicit separation between:

- scoped task instructions
- background project context
- read-only reference material
- writable artifact areas

## Current State

### Current Prompt Assembly

At runtime, Tulid currently assembles the prompt in this order:

1. runtime preamble from `src/open_tulid/runtime/executor.py`
2. current task body
3. full parent task bodies
4. linked context packet from `src/open_tulid/runtime/context.py`
5. instruction packet from `src/open_tulid/runtime/instructions.py`

The main call path is:

- `JobExecutor.run()`
- `_build_runtime_prompt()`
- `_append_parent_tasks()`
- `LinkedContextResolver.build_context_packet()`
- `AgentInstructionResolver.build_prompt_packet()`

### Current Weaknesses

The current packet is syntactically structured, but not strongly prioritized.

Problems:

- the task is not clearly declared as the primary authority
- parent and spec material are large and feel co-equal to the task
- the worker is told to read `.open-tulid/job-context.json`, but that does not override the broader packet hierarchy
- `output/` is mentioned as the artifact location, but also contains planning documents that the task references
- the prompt does not strongly define what background context is for
- the prompt does not strongly define what the worker must *not* do during implementation transitions

## Design Principles

The new prompt architecture should follow these principles.

### 1. Transition-Specific Prompt Roles

Tulid should not treat all execution transitions as prompt-equivalent.

`ImplementTask` is fundamentally different from:

- `DraftDirection`
- `WriteImplementationSpec`
- `BreakDownImplementationSpec`

Planning transitions are allowed to synthesize broad project structure.

Implementation transitions are not.

Therefore Tulid should generate transition-class-specific prompt framing, even if the underlying assembly pipeline remains shared.

### 2. Explicit Context Priority

Every implementation prompt should state a strict authority order.

For example:

1. current transition objective
2. current task body and acceptance criteria
3. completion contract and validation requirements
4. parent/spec/context documents as background reference only

This hierarchy should appear explicitly in the prompt text, not be implied by order alone.

### 3. Explicit Read-Only vs Writable Semantics

The prompt must state which materials are:

- reference only
- implementation targets
- completion-artifact destinations

This is especially important because the worker currently encounters planning documents in paths that visually resemble writable output space.

### 4. Narrow-Task Framing Must Be Repeated

It is not enough to say once that the worker has one task.

The prompt should reinforce narrowness in several places:

- role statement
- primary objective
- context priority section
- forbidden scope expansion statement
- completion contract

### 5. Prompt-Level Changes First

This phase deliberately avoids stronger runtime behavior like:

- output validation
- context pruning
- parent summarization
- context summarization

The purpose of this phase is to improve behavior through a better prompt contract alone, while keeping runtime behavior easy to reason about and easy to diff.

## Scope of This Plan

This plan includes:

- redesign of the runtime preamble text
- new section layout for prompt packets
- transition-specific wording for implementation transitions
- explicit read-only context language
- explicit context-priority language
- clearer completion-submission instructions
- small structural changes in how prompt text is ordered

This plan excludes:

- changing what files are loaded as linked context
- changing how parent context is summarized
- adding new validators or rejection rules
- moving files on disk as a hard requirement for this phase

Note:

The prompt may still *refer* to `.open-tulid/context/` as the desired future read-only location, but this plan does not require implementing new context copying behavior yet. The prompt-writing work should stand on its own.

## Proposed Prompt Model

### High-Level Packet Shape

For implementation transitions, the prompt should be reorganized into stable sections:

1. `# Open Tulid Job`
2. `## Role`
3. `## Primary Objective`
4. `## Context Priority`
5. `## Read-Only And Writable Paths`
6. `## Completion Contract`
7. `## Task Body`
8. `## Parent Context`
9. `## Linked Context`
10. `## Instructions`

Not every section needs to be present for every transition, but `ImplementTask` should use the full hierarchy.

### Section Semantics

#### `## Role`

This section defines what the worker is doing in one sentence.

For `ImplementTask`, it should say that the worker is implementing one scoped task inside an existing plan, not rewriting the plan.

Example intent:

- you are implementing one already-derived task
- do not switch into planning mode
- do not broaden scope beyond this task

#### `## Primary Objective`

This section should describe the exact completion condition for the transition.

For `ImplementTask`, this means:

- make the code changes required by the current task
- satisfy required validations
- submit completion evidence

It should be explicit that success is not producing more planning artifacts.

#### `## Context Priority`

This is the most important structural change.

The prompt should enumerate a strict priority order, such as:

1. the current task body is the authoritative scope boundary
2. required validations and completion requirements are mandatory
3. parent and linked context are background reference only
4. when reference material conflicts with the current task scope, do not broaden scope; stay within the task

This section should make it obvious that the worker is not being asked to act on every piece of context equally.

#### `## Read-Only And Writable Paths`

This section should make path semantics explicit.

For this prompt-only phase, Tulid should state:

- planning/spec files provided for reference are read-only context
- source files and test files in the workspace are writable implementation targets
- `output/` is only for required completion artifacts
- if no artifacts are required, `output/` should not be used except where Tulid explicitly requires it

This section should not yet depend on new runtime enforcement.

#### `## Completion Contract`

This section should state:

- what must be submitted
- when `artifacts` must be empty
- what `changed_files` means
- what validation evidence must contain
- that completion is not implicit from process exit code

This should reduce the chance that a worker assumes “worked in the workspace” is enough.

#### `## Task Body`

This remains the main scoped work definition.

For implementation transitions, it should be visually and structurally positioned as the dominant content block.

#### `## Parent Context`

For this phase, keep full parent task injection, but demote it semantically by:

- renaming the section from `Parent Task` to `Parent Context`
- prefixing it with text that it is background only
- clarifying that it provides project intent, not scope expansion authority

This preserves functionality while improving hierarchy.

#### `## Linked Context`

This should remain clearly labeled as linked reference material.

The section header should signal that these documents support implementation decisions but do not redefine the assigned task.

#### `## Instructions`

Instructions from `agents/` should remain as the final layered guidance packet.

This ordering is acceptable as long as the earlier runtime sections already define the hierarchy and scope strongly.

## Transition-Specific Prompt Variants

Tulid should introduce runtime preamble variants by transition class.

### Variant A: Planning Transitions

Applies to:

- `DraftDirection`
- `WriteImplementationSpec`
- `BreakDownImplementationSpec`

Characteristics:

- broad synthesis allowed
- output artifact generation expected
- planning context is primary, not secondary

### Variant B: Implementation Transitions

Applies to:

- `ImplementTask`
- likely self-review implementation passes

Characteristics:

- narrow implementation only
- current task is primary authority
- reference docs are secondary
- output artifacts are usually none
- completion requires explicit submission

This plan is mostly about Variant B.

## Exact Prompt-Writing Changes

### Change 1: Replace Generic Preamble With Hierarchical Preamble

Current prompt opening is operationally correct but too generic.

Replace it with a hierarchy-aware block that:

- names the transition class
- states the role
- states the objective
- states the scope boundary

### Change 2: Add Explicit Context Priority Section

This should be implemented in Tulid runtime prompt construction, not in project-level agent files.

Reason:

- this hierarchy is a runtime concern
- it should be consistent across projects
- project prompts should not have to compensate for weak runtime structure

### Change 3: Add Explicit Read-Only Context Language

This is the one “spec-read-only” change included in this phase.

The prompt should say that planning docs are read-only reference material.

Important nuance:

This phase changes prompt language only.

It does not yet change:

- linked-context loading
- file placement
- validation behavior

### Change 4: Make Empty-Artifacts Behavior First-Class

When a transition requires no artifacts, the prompt should explicitly say:

- `artifacts` must be `[]`
- do not invent output artifacts
- completion success is based on code changes and validation evidence

### Change 5: Rename Parent Injection Semantically

Even if the parent task body stays full-size for now, the prompt should stop framing it as a co-equal task-like authority.

Suggested rename:

- from `## Parent Task 1`
- to `## Parent Context 1`

And prepend one clear line:

- this section is background project context, not an instruction to broaden the assigned task

### Change 6: Label Linked Context As Reference Material

Likewise, linked context should be framed as:

- relevant reference material
- useful for implementation details
- not a replacement for the current task scope

## What This Plan Deliberately Does Not Solve Yet

### No Parent Summaries Yet

We are explicitly not introducing:

- auto-generated summaries
- task-scoped section extraction
- selective semantic compression

Reason:

- higher implementation complexity
- harder to verify correctness
- larger change surface than prompt-structure-only work

### No Forbidden-Output Validation Yet

We are explicitly not introducing:

- rejection of `output/product-spec.md`
- rejection of `output/implementation-spec.md`
- rejection of `output/tasks/*`

Reason:

- the current request is to improve prompt structure only
- enforcement should be handled as a separate runtime hardening phase

### No Reduced Context Set Yet

We are not yet reducing:

- parent task size
- linked context size
- number of referenced planning files

This phase only changes how the model is told to interpret that context.

## Implementation Plan

### Step 1: Refactor Runtime Prompt Builder

File:

- `src/open_tulid/runtime/executor.py`

Tasks:

- split `_build_runtime_prompt()` into clearer sub-builders or templated branches
- add transition-class-aware preamble generation
- add stable section rendering helpers

Recommended internal structure:

- `_build_runtime_prompt()`
- `_runtime_prompt_variant()`
- `_render_prompt_role_section()`
- `_render_prompt_priority_section()`
- `_render_prompt_paths_section()`
- `_render_prompt_completion_section()`

This is a refactor for clarity, not just text substitution.

### Step 2: Add Implementation-Transition Prompt Variant

Define a branch for implementation-style transitions.

Minimum classification rule for this phase:

- if `derived_artifact_type` is `None` and transition looks like a code execution transition, use implementation framing

Preferably:

- classify from transition id or worker role more explicitly if safe

But avoid a large workflow-schema change in this phase.

### Step 3: Change Parent Section Labeling

Update `_append_parent_tasks()` so the emitted section name and preface reflect background context semantics.

Do not summarize yet.

Only change framing.

### Step 4: Add Linked-Context Section Framing If Needed

If the linked context packet currently has a generic heading, update it so the heading clearly implies:

- reference
- background
- not scope authority

This may require small changes in `src/open_tulid/runtime/context.py` if the packet text is assembled there.

### Step 5: Keep Instruction Layering Intact

Do not change instruction resolution order in this phase.

Keep:

- worker instructions
- task-type instructions
- transition instructions

The issue is not resolution order; it is missing runtime hierarchy around them.

## Testing Plan

This change should be tested mostly through prompt-packet assertions.

### Unit Tests

Primary file:

- `tests/runtime/test_executor.py`

Add or update tests for:

- implementation transition prompt includes explicit role section
- implementation transition prompt includes explicit context priority section
- implementation transition prompt includes explicit read-only path language
- implementation transition prompt includes explicit empty-artifacts guidance
- parent context header is demoted from task-like authority
- planning transitions still retain planning-oriented wording

### Non-Goals For Tests In This Phase

Do not require tests for:

- semantic model behavior
- file-output rejection
- context summarization
- worker compliance beyond prompt content

Those belong to later phases.

### Regression Risk To Watch

The prompt-assembly tests in `tests/runtime/test_executor.py` are currently brittle because they often assert exact strings.

This prompt architecture change will likely require:

- updating string assertions
- converting some tests to section-presence assertions instead of exact paragraph snapshots

That should be done deliberately, not as incidental churn.

## Rollout Strategy

### Phase 1

Implement prompt-writing changes only.

Deliverables:

- new runtime section hierarchy
- implementation-transition-specific wording
- read-only context language
- stronger completion contract wording

### Phase 2

After observing real runs, decide whether prompt-only changes are enough.

If not, the next likely phases are:

- parent/spec summarization
- read-only context relocation
- forbidden-output validation

Those phases should remain separate from this one.

## Expected Outcomes

If this prompt-only phase works, we should see:

- fewer implementation runs drifting into planning behavior
- fewer broad planning artifacts created during `ImplementTask`
- clearer completion submissions
- better model adherence to the assigned task boundary

If it does not fully work, the likely conclusion will be:

- prompt structure improved salience, but structural runtime separation is still needed

That would justify the later enforcement and context-shaping phases.

## Acceptance Criteria For This Design Phase

This design phase is complete when:

- Tulid generates a visibly hierarchical prompt for implementation transitions
- the prompt explicitly states context priority
- the prompt explicitly states read-only versus writable semantics
- the prompt explicitly states that no-artifact transitions submit `artifacts: []`
- parent and linked context are framed as reference material rather than co-equal task authority
- tests verify the presence of these sections and rules

## Summary

This plan keeps the change focused and low-risk.

It treats the problem as a prompt-architecture issue inside Tulid itself, which is the correct abstraction boundary.

It avoids premature complexity from:

- summarization
- semantic extraction
- validators
- enforcement

And it gives Tulid a stronger execution contract for implementation work without changing workflow definitions or project prompt files.
