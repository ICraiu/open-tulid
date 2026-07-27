# Qwen Prompt Quality Improvement: Implementation Plan

## Purpose

This plan turns the findings in `qwen-task-2-prompt-analysis.md` into changes
to Open Tulid.

The goal is a large improvement in the quality of work produced by the Qwen
implementation worker. The main lever is not stronger wording. It is a cleaner
division of responsibility:

- the user may create a task in any useful form, from one sentence to a long
  Markdown brief, without learning a Tulid contract schema;
- the high-level model makes product, architecture, interface, scope, and test
  decisions and translates the user's task into an execution contract;
- Tulid rejects incomplete or contradictory work orders before Qwen runs;
- Qwen receives one compact, decision-complete implementation contract;
- Tulid independently verifies the exact contract shown to Qwen;
- self-review receives the produced change and evidence, not another generic
  implementation prompt.

The target result is that Qwen spends its capacity implementing a bounded
design rather than reconstructing the design from large planning documents,
resolving contradictory instructions, or guessing how success will be tested.

### User task freedom is a hard requirement

The execution-contract schema is an internal protocol between Tulid and its
LLM workers. It is not the user-facing task format.

Users must remain free to create:

- a one-line bug report;
- an exploratory idea;
- a detailed product brief;
- a task with arbitrary headings and prose;
- a task imported from another tracker;
- a manually curated task that follows no Tulid template.

Tulid must not make the user populate `allowed_paths`, signatures, Panalyzer
IDs, validation arrays, hashes, or budgets. The high-level model prepares those
fields from the task, repository, project artifacts, and workflow. If a
material product decision genuinely cannot be derived, Tulid asks for that
decision; it does not ask the user to restructure the whole task.

Acceptance example:

```text
User creates:
  title: Fix status command crash
  body: It crashes when no service is installed.

Tulid:
  preserves that text
  -> prepares and validates an internal execution contract
  -> schedules Qwen with the compiled contract
```

## Target execution path

```text
User-authored task
  any structure; intent remains unchanged
                         |
                         v
High-level planning/contract model
  reads task + repo + relevant project context
  makes or reuses technical decisions
                         |
                         v
Generated execution-contract draft
  normalized internal structure; user did not author it
                         |
                         v
Tulid generated-contract validator
  returns defects to the high-level model for correction
                         |
                         v
Tulid execution-contract compiler
  freezes task + repository facts + validations + selected context
                         |
                         v
Tulid prompt compiler
  emits one ordered, deduplicated Qwen packet
                         |
                         v
Qwen implementation
  inspect -> implement -> focused checks -> invariants -> submit
                         |
                         v
Tulid trusted verifier
  checks real changed paths and reruns the frozen validation contract
                         |
                         v
Qwen self-review
  contract-to-change audit -> targeted correction or valid no-op
```

For a large product task, the direction, specification, and breakdown stages
still run before contract preparation. For a direct bug or maintenance task,
Tulid may prepare the contract in one high-level-model pass. Both paths produce
the same internal input for Qwen.

## Architectural decisions

### 1. User task and execution contract are separate representations

The user-authored `Task` remains the source of intent. Tulid never requires its
body or frontmatter to match an execution schema.

The high-level model produces a separate versioned
`ImplementationContractDraft` artifact. Tulid validates and compiles that
artifact into `.open-tulid/execution-contract.json` for the Qwen job. The
compiled contract records the source task ID and hash, but it does not replace
or rewrite the user's task text.

There are two automatic production paths:

- the breakdown model may emit a contract draft alongside each derived
  implementation task, which avoids repeating decisions it just made;
- a user-created or imported implementation task without a contract is routed
  through a `PrepareExecutionContract` high-level-model transition.

Users may optionally supply precise constraints in task prose or metadata.
The contract authoring model should preserve them, but those fields are hints
and inputs—not a required user schema.

For the first implementation, it is acceptable to route every implementation
task through `PrepareExecutionContract`; reuse of breakdown-generated drafts is
an optimization, not a user-facing requirement.

### 2. Prompt rendering and verification use the same frozen contract

The scheduler must compile an `ExecutionContract` before it creates a Qwen job.
That object is stored in job metadata and written into the workspace. The
prompt renderer and completion verifier must both consume that object.

Neither component may independently rediscover commands or scope from prose.
This prevents a prompt from showing one command while the verifier runs
another.

The contract should be content-addressed and include:

- generated-contract schema and hash;
- task snapshot and source-intent hash;
- planning artifact and Panalyzer proposal hashes;
- repository base commit and repository-facts hash;
- resolved allowed and forbidden paths;
- resolved targeted checks and project invariants;
- selected context excerpts and their hashes;
- transition, worker, and instruction hashes;
- prompt-compiler version.

The source-intent hash must cover user-owned task content and constraints, such
as title, body, dependencies, and custom metadata. It must exclude
workflow-owned state, generated artifact links, and runtime metadata; otherwise
the normal `Todo -> ReadyToImplement` move would invalidate the contract that
caused it.

### 3. Every prompt concern has one owner

Use structural ownership instead of hoping text-level deduplication will repair
concatenated prompts:

| Concern | Single owner |
|---|---|
| Objective, behavior, interfaces, scope | Generated execution contract |
| Repository state | Repository-facts compiler |
| Validation commands | Resolved execution contract |
| Work sequence and bounded repair behavior | Qwen execution policy |
| Completion endpoint and payload | Runtime completion section |
| Planning background | Explicit context excerpts |
| Paths and hashes used for audit | Prompt manifest, not model text |

The prompt compiler must treat `completion`, `validation`, `scope`, and
`execution-procedure` as singleton section IDs and reject a packet that tries
to emit one twice.

### 4. Context selection is explicit and deterministic

Implementation prompts will no longer recursively include the full parent body
and all linked artifacts.

The contract-authoring model must make each execution contract self-contained.
When an excerpt is genuinely required, it declares an artifact and a Markdown
heading. Tulid extracts that section deterministically, records its hash and
selection reason, and enforces a budget.

Do not add model-generated summaries in the first implementation. A
deterministic excerpt is inspectable and reproducible; an extra summarization
model introduces another source of drift.

### 5. Tulid, not Qwen, determines acceptance

Qwen runs checks for feedback, but its `validation_evidence` is not proof.
Tulid reruns every resolved check in a trusted verification workspace and
derives the changed-file set from a pre-worker manifest.

The completion payload remains useful for the implementation summary and
diagnosis, but it cannot weaken scope or validation.

## Generated, versioned implementation contract

### Internal artifact, not a task template

The first schema should be intentionally narrow, but it is emitted by the
high-level contract model—not written by the user:

```yaml
schema: tulid.implementation/v1
source:
  task_id: "2"
  source_intent_sha256: "..."
profile: bootstrap
objective: Create the installable package and runnable CLI root.
base:
  implementation_spec_sha256: "..."
  panalyzer_proposal_sha256: "..."
change_surface:
  add:
    - pyproject.toml
    - src/voiceflow_local/__init__.py
    - src/voiceflow_local/cli/main.py
    - tests/unit/cli/test_main.py
  edit:
    - .gitignore
  forbidden:
    - README.md
    - src/voiceflow_local/runtime/**
interfaces:
  - symbol: voiceflow_local.cli.main.run
    signature: "run(argv: Sequence[str] | None = None) -> int"
    behavior: Parse arguments and dispatch the selected command.
requirements:
  - Root --help exits 0 and lists all required commands.
  - Placeholder commands return the planner-selected exit code and message.
failure_behavior:
  - Importing the CLI performs no filesystem, network, or subprocess work.
non_goals:
  - Service lifecycle
  - STT, LLM, audio, clipboard, and hotkey behavior
checks:
  focused:
    - id: cli_unit
      argv: [uv, run, pytest, -q, tests/unit/cli/test_main.py]
      timeout_seconds: 120
      expect:
        exit_code: 0
  invariants:
    - cli_help
    - package_build
panalyzer:
  files:
    - src/voiceflow_local/cli/main.py
  methods:
    - voiceflow_local.cli.main.run
  references: []
context_excerpts:
  - artifact: ImplementationSpec
    heading: CLI contracts
    reason: Exact command and dispatch behavior
budget:
  max_changed_files: 8
  max_changed_production_lines: 150
```

Tulid stores this generated draft as a task-linked
`ImplementationContract` artifact. At job creation it combines the draft with
deterministic repository and workflow data to create the immutable runtime
contract.

The user-authored title, body, and custom metadata remain unchanged. Tulid may
update workflow-owned state and artifact links, show the generated contract for
inspection, and allow a user to override an individual decision, but it must
not require the user to maintain the contract format.

### Flexible contract profiles

The internal schema is uniform enough for Qwen and the verifier, but not every
field is mandatory for every kind of work.

Every Qwen code-execution contract requires only:

- one observable objective;
- a resolved change boundary;
- at least one executable acceptance check.

The high-level model selects a profile such as:

- `bootstrap`;
- `code_change`;
- `bug_fix`;
- `refactor`;
- `test_only`;
- `configuration`;
- `documentation`;
- `integration`.

Profiles add conditional requirements. For example, a public API change needs
exact interfaces, a documentation task needs Markdown in its allowed surface,
an integration task needs project invariants, and a test-only task must forbid
production edits. Panalyzer slices, context excerpts, explicit failure
behavior, and custom budgets are included only when relevant. Tulid supplies
safe defaults for omitted budgets and runtime policy.

This preserves task variety while giving Qwen a stable execution vocabulary.

### Contract validation

Create a typed `ImplementationContractDraft` parser and validator. Validation
must happen in two places:

1. when either the breakdown model or `PrepareExecutionContract` model submits
   the generated artifact;
2. immediately before scheduling an implementation or self-review transition,
   so stale generated contracts cannot bypass the gate.

The validator must report stable, actionable diagnostics for:

- unsupported or missing generated-contract schema;
- ambiguous or unbounded objectives;
- a missing resolved change surface or profile-required path category;
- overlap between allowed and forbidden paths;
- unsafe absolute paths or `..` traversal;
- missing interfaces or failure behavior when the selected profile requires
  them;
- acceptance requirements with no focused check or invariant;
- duplicate check IDs;
- missing or unknown invariant IDs;
- empty argument vectors, shell control operators, or unsupported executables
  in checks;
- invalid timeouts or expected exit/output assertions;
- unresolved Panalyzer identities when a Panalyzer slice is present;
- stale implementation-spec or proposal hashes;
- a repository base incompatible with the declared base;
- a command/toolchain contradiction, such as npm checks with no existing or
  task-owned `package.json`;
- missing context artifacts or headings;
- budgets exceeded by the declared surface;
- unresolved template markers or placeholder text.

The contract-producing completion should be rejected with these diagnostics
while the high-level worker is still alive, allowing it to correct and
resubmit the contract in the same job.

If a user-authored task has no contract, that is not an error. Tulid schedules
contract preparation. If the high-level model finds an unresolved decision
that would materially change the user's requested outcome, it emits a concise
`human_decision_required` diagnostic containing only the decision needed.

## Changes to the high-level model's work

### Adaptive contract preparation

Add a high-level worker instruction
`execution-contract-authoring.agent.md`. It receives:

- the user's task verbatim;
- current repository facts;
- project direction/specification artifacts when they exist;
- applicable workflow invariants;
- the relevant Panalyzer view when available;
- prior human constraints and dependency results.

Its output is only the `ImplementationContract` artifact. It must not rewrite
the user's task, manufacture unrelated product scope, or produce implementation
code.

Choose the cheapest planning route that can produce a sound contract:

- a narrow, well-grounded bug or maintenance task uses one contract-authoring
  pass;
- a derived task reuses the contract draft emitted by breakdown;
- a broad feature follows direction, specification, breakdown, then contract
  validation;
- an exploratory idea remains in planning and is not forced into a Qwen code
  contract prematurely.

This decision is based on workflow state and task intent, not on whether the
user followed a template.

### Direction authoring

Update `technical-direction-template.agent.md` and
`direction-authoring.agent.md` so the direction stage locks project-wide
choices that Qwen must never have to invent:

- language and supported runtime versions;
- package/build backend;
- key libraries and framework choices;
- application entrypoint and composition-root strategy;
- public compatibility constraints;
- error and exit-code conventions;
- deterministic project invariant commands;
- external boundaries that must be faked in normal end-to-end tests;
- decisions intentionally deferred and the stage that owns them.

A direction artifact may contain open questions, but a question that affects
an implementation contract must be resolved before the product task advances
to implementation specification.

Add an artifact validation that distinguishes informational open questions
from blocking design decisions.

### Implementation specification

Expand `implementation-spec-template.agent.md` and
`implementation-spec.agent.md` from descriptive architecture guidance into a
code-ready contract for the breakdown stage.

Require:

- a factual repository inventory at the inspected base commit;
- a module ownership table;
- exact public symbols and signatures;
- data shapes and state transitions;
- behavior and failure behavior for each interface;
- invariants that every task must preserve;
- deterministic validation profiles and expected observations;
- Panalyzer proposal identities when a proposal is available;
- dependency and integration seams;
- a list of decisions that must be copied into child contracts;
- explicit confirmation that no implementation-blocking design decision is
  unresolved.

Remove incentives that reduce task quality:

- no fixed number of tasks per module;
- no target number of Markdown lines per task;
- no mandatory diagram in every child task;
- no use of a 500-line ceiling as a normal task size.

Diagrams remain useful in the implementation specification when they clarify
state, data flow, or ownership. They should not consume Qwen task context by
default.

### Task breakdown

Rewrite `task-breakdown.agent.md` around contract generation rather than prose
decomposition.

The high-level breakdown worker must:

1. inspect the current repository and the implementation specification;
2. resolve the exact proposal slice and integration seam for each task;
3. divide work by one observable behavior, not by file quotas;
4. produce a dependency DAG;
5. copy all relevant design decisions into each contract;
6. name exact allowed paths, interfaces, failure behavior, checks, and
   non-goals;
7. create a separate integration task when a user-visible flow crosses several
   component tasks;
8. emit a generated contract draft alongside each child task;
9. run a contract checklist before submitting artifacts.

The prompt should explicitly require a split when Qwen would otherwise need to:

- choose architecture or a framework;
- invent a public interface;
- coordinate multiple independent callers;
- change more than three production files under normal circumstances;
- exceed roughly 150 changed production lines;
- introduce and integrate several unrelated behaviors.

Those numbers are default heuristics, not hard universal limits. The contract
may request a justified exception, which Tulid records and exposes during
review.

The child task body remains human-readable and may use whatever structure best
communicates the work. Only the paired internal contract artifact follows the
machine schema.

### Planner feedback loop

Do not route contract errors to Qwen. Return them to the planning transition
that owns the decision.

Diagnostics should say what is missing and where it must be fixed, for example:

```text
task.contract.validation_uncovered [cli-bootstrap.requirements[2]]:
Requirement "status exits 2 with an exact message" is not exercised by a
focused check or project invariant.
```

This makes the high-level model improve the work order before local-model
tokens are spent. The user sees a concise question only when the model cannot
safely resolve a material product choice from existing context.

## Tulid implementation workstreams

### Workstream A: contract domain and automatic contract preparation

Add `src/open_tulid/runtime/task_contracts.py` containing:

- `ImplementationContractDraft`;
- nested value types for change surface, interfaces, checks, Panalyzer slice,
  context excerpts, source hashes, and budgets;
- profile-aware required-field rules;
- `parse_implementation_contract_artifact(...)`;
- `validate_implementation_contract(...)`;
- stable diagnostic codes and locations.

Add an `ImplementationContract` artifact type and a
`PrepareExecutionContract` transition backed by the high-level model. A
user-created or derived implementation task remains in the familiar `Todo`
intake state; a valid contract moves it to `ReadyToImplement`, where Qwen
becomes schedulable.

Conceptually, the default workflow becomes:

```yaml
- kind: transition
  id: PrepareExecutionContract
  task_type: ImplementationTask
  from: Todo
  to: ReadyToImplement
  worker: codex_contract
  requires:
    artifacts: [ImplementationContract]

- kind: transition
  id: ImplementTask
  task_type: ImplementationTask
  from: ReadyToImplement
  to: SelfReview
  worker: qwen_27b
```

Store the generated contract as a linked artifact with its source task ID and
source-intent hash. The task body remains unchanged. If user-owned intent
changes, invalidate the contract and schedule preparation again. Workflow state
changes and linking the generated artifact must not invalidate it.

When a `ReadyToImplement` task has a stale source-intent hash, TaskManager must
record a `ContractInvalidated` event and move it back to `Todo` through an
explicit system transition. The stale artifact remains available for audit,
but it is never given to Qwen.

For breakdown-generated tasks, initially use the same preparation transition.
A later optimization may let breakdown submit task/contract pairs atomically,
provided Tulid validates each pair and preserves the user's freedom to create
unstructured tasks through the normal preparation path.

Keep the adapter generic: `ObsidianAdapter` already round-trips arbitrary
metadata and artifact links and should not contain implementation-contract
rules.

Tests:

- a one-line user task can enter contract preparation without metadata;
- arbitrary Markdown bodies and unrelated frontmatter survive preparation;
  only workflow-owned state and artifact-link fields change;
- a valid v1 artifact links to its source task and round-trips through storage;
- profile-required fields have focused diagnostics;
- unknown nested fields are handled according to the schema policy;
- invalid paths and overlapping boundaries are rejected;
- a changed source task invalidates its old contract;
- workflow state and generated artifact-link changes do not alter the
  source-intent hash;
- an invalid draft is returned to the high-level worker for correction;
- an irreducibly ambiguous task yields one concise human decision request;
- existing unstructured tasks are queued for automatic contract preparation,
  not rejected or marked permanently non-executable.

### Workstream B: readiness and immutable execution contracts

Add `src/open_tulid/runtime/execution_contracts.py` with:

- `RepositoryFacts`;
- `ResolvedCheck`;
- `SelectedContextExcerpt`;
- `ExecutionContract`;
- `ExecutionReadinessResult`;
- a compiler that merges the generated contract, source task, transition,
  workflow invariants, repository facts, and instruction/context hashes.

Call the compiler from `TaskManager.create_execution_job()` before persisting a
job. Store the serialized contract and its SHA-256 in job metadata. For prompt
preview, call the same compiler in read-only mode with a synthetic job ID.

Do not silently recompile an existing job at execution or completion time. A
job must use the frozen contract it was created with. If the contract cannot be
loaded or its hash fails, fail the job as corrupt.

Extend the workflow definition with project invariant declarations. Keep
existing static transition validations working during migration, but compile
them into the same `ResolvedCheck` representation.

Suggested invariant declaration:

```yaml
- kind: project_invariant
  id: cli_help
  validation: tests_pass
  args:
    argv: [uv, run, voiceflow-local, --help]
    timeout_seconds: 30
    expect:
      exit_code: 0
```

Task contracts select invariant IDs; focused checks remain task-owned. Tulid
validates both before job creation and freezes the fully resolved commands.

Represent commands as argument arrays, not shell strings. Extend the workflow
schema and command runner accordingly. If a project needs pipes, redirects, or
multi-command setup, it must call a version-controlled script that is itself in
the task/repository contract. Do not pass planner-authored text through a shell.

`ResolvedCheck` must support:

- argument vector;
- working directory relative to the workspace;
- timeout;
- allowlisted environment additions;
- expected exit code;
- optional bounded stdout/stderr substring assertions.

This is required to validate intentional nonzero behavior, such as an
unimplemented placeholder command exiting `2`, without treating it as a failed
check.

Tests:

- scheduler skips an incomplete or stale contract with precise diagnostics;
- prompt preview and scheduled execution produce the same contract hash for
  the same inputs;
- editing workflow or task inputs after scheduling does not mutate the job;
- unknown invariant IDs are rejected;
- task checks and workflow checks cannot silently use the same ID with
  different commands;
- quoted arguments and paths with spaces survive resolution unchanged;
- shell operators in planner-authored checks are rejected;
- expected nonzero exits and output assertions are evaluated correctly;
- npm/Python manifest contradictions fail readiness;
- legacy non-Qwen transitions continue to work.

### Workstream C: repository facts and baseline manifest

Add `src/open_tulid/runtime/repository_facts.py`.

At job creation or workspace preparation, capture deterministic facts:

- repository commit when available;
- clean/dirty state;
- tracked top-level files;
- detected manifests such as `pyproject.toml`, `package.json`, `Cargo.toml`,
  and `go.mod`;
- presence or absence of contract-owned paths;
- existing test/build entrypoints;
- a hash manifest of workspace files before the worker runs.

Render only facts relevant to the current contract. Store the full facts and
file manifest under `.open-tulid/`.

Facts must describe observations, not make design decisions. For example,
Tulid may say "`package.json` is absent"; the planner contract must say whether
the task owns creating it.

Tests:

- empty bootstrap repository facts are accurate;
- missing allowed additions are reported as intentional additions;
- missing allowed edits are rejected;
- ignored runtime/cache paths are excluded from the baseline manifest;
- additions, edits, removals, and renames are detected without relying on a
  copied `.git` directory.

### Workstream D: structured prompt compiler

Create `src/open_tulid/runtime/prompts.py` and move model-facing prompt assembly
out of `runtime/executor.py`.

Use typed prompt sections:

```python
@dataclass(frozen=True)
class PromptSection:
    id: str
    title: str
    text: str
    source_kind: str
    source_ref: str
    sha256: str
    singleton: bool = True
```

The compiler should produce this implementation packet order:

1. `Mission`;
2. `Repository Facts`;
3. `Execution Contract`;
4. `Panalyzer Proposal Slice`, when present;
5. `Selected Reference Excerpts`, when present;
6. `Required Validation`;
7. `Execution Procedure`;
8. `Completion Submission`.

The generated execution contract—not the original free-form task body—must
occupy the highest priority position in Qwen's packet. Relevant rationale from
the user task can appear at the end of that section, under a label that cannot
expand the compiled scope.

Enforce starting budgets:

| Section | Starting limit |
|---|---:|
| Execution contract and task rationale | 2,500 words |
| Repository facts | 300 words |
| Panalyzer slice | 800 words |
| Selected excerpts | 2,000 words total |
| Runtime policy and completion | 500 words |
| Whole implementation packet | 6,000 words |

A contract that cannot fit should fail readiness and return to contract
preparation or the planning stage that owns it. Truncating an interface,
requirement, or validation command is not allowed.

Generate the completion example from actual required IDs:

```json
{
  "summary": "Implemented the scoped CLI bootstrap.",
  "artifacts": [],
  "changed_files": ["pyproject.toml"],
  "validation_evidence": {
    "cli_unit": "uv run pytest -q tests/unit/cli/test_main.py: exit 0",
    "cli_help": "uv run voiceflow-local --help: exit 0"
  }
}
```

There must be one completion explanation and one curl block, at the end of the
packet.

Tests:

- singleton prompt sections cannot be duplicated;
- the curl block occurs exactly once;
- every required command and validation ID occurs exactly once in operative
  sections;
- generated execution contract precedes generic policy;
- audit paths and hashes do not appear in model text;
- the prompt remains deterministic for identical inputs;
- prompt preview and actual execution use the same compiler;
- budget errors identify the offending section.

### Workstream E: instruction and context cleanup

Change `AgentInstructionResolver` so model text contains a logical source label
and normalized instruction content only. Keep absolute paths and SHA-256 values
in `.open-tulid/prompt-manifest.json`.

Reduce `qwen-implementation.agent.md` to Qwen-specific work behavior:

1. read the mission, scope, interfaces, and checks;
2. inspect named paths and seams before editing;
3. make the smallest coherent implementation;
4. run the narrowest focused check;
5. run project invariants;
6. compare actual changes with the allowed surface;
7. submit evidence or stop on a classified blocker.

Remove from the agent file:

- completion mechanics;
- validation command lists;
- generic scope warnings already represented by exact boundaries;
- the runtime-owned validation failure policy;
- blanket Markdown prohibitions.

Replace the Markdown rule with:

> Do not create planning reports or unrequested documentation. A Markdown file
> may be edited only when it is in the allowed change surface and a requirement
> calls for that edit.

Change `LinkedContextResolver` to resolve only declared context excerpts for
implementation contracts. Preserve the existing recursive behavior for
planning transitions until those prompts are migrated.

The excerpt resolver must:

- resolve the exact artifact;
- locate the exact requested heading;
- stop at the next heading of equal or higher level;
- reject missing or duplicate headings;
- enforce per-excerpt and total budgets;
- record the selection reason and source hash.

Remove `_append_parent_tasks()` from implementation prompt construction. A
compact parent title and objective may be included only if the generated
execution contract does not already contain them.

### Workstream F: trusted validation and scope enforcement

Change `DeterministicVerifier.verify()` to accept the frozen
`ExecutionContract` and the actual `Task`.

Run checks in this order:

1. compare the post-worker workspace with the pre-worker file manifest;
2. reject files outside the allowed surface;
3. enforce addition/edit/removal and generated-file policy;
4. enforce max-file and changed-line budgets;
5. run focused checks;
6. run project invariants;
7. validate completion artifacts and evidence shape.

The real manifest diff is authoritative. Do not skip scope enforcement when
`.git` is absent, and do not trust only Qwen's `changed_files` list.

Capture for every check:

- ID and class (`focused` or `invariant`);
- argument vector and display form;
- exit code;
- duration;
- bounded stdout/stderr excerpt;
- pass/fail result;
- failure classification when known.

Run checks without a shell, with the frozen working directory, environment
allowlist, timeout, and output expectations. Replace the current whitespace
splitting in `_command_arg()`; it corrupts quoted arguments and is not suitable
for a machine-checkable contract.

Return structured feedback to the still-running Qwen job:

- `implementation_failure`: eligible for a targeted repair;
- `contract_failure`: stop and return to planning;
- `baseline_failure`: stop without asking Qwen to edit;
- `environment_failure`: block without consuming a repair attempt.

Enforce the repair limit in Tulid job state rather than only mentioning it in
the prompt.

### Workstream G: task-specific self-review

The self-review packet must be compiled as a distinct packet type. Include:

- the frozen implementation contract;
- authoritative changed-file list;
- compact diff or per-file diff summary;
- focused and invariant results from implementation;
- acceptance requirements not directly proven by a check;
- any completion rejection/repair history.

The review procedure is:

1. map each requirement to code or test evidence;
2. inspect changed files and named interfaces;
3. identify a concrete in-scope defect, if any;
4. make only a targeted correction;
5. rerun affected focused checks and all invariants;
6. submit a patch or an accepted no-change review.

Set `changed_files.required: false` for `SelfReview`. When the review makes a
change, Tulid still derives and promotes the real changed paths. When it finds
no defect, an empty diff with fresh trusted validation is valid.

Persist the implementation change manifest and validation report so a new
self-review workspace can reconstruct what the prior job changed even after
the implementation was promoted.

### Workstream H: prompt inspection, linting, and history

Build on `tulid prompts render` with:

```bash
tulid prompts explain PROJECT TASK --transition ImplementTask
tulid prompts lint PROJECT TASK --transition ImplementTask
tulid prompts show-job PROJECT JOB_ID
```

`explain` reports section order, source kind, size, selection reason, hashes,
budgets, and whether inputs are live or historical.

`lint` reports:

- duplicate singleton sections or normalized blocks;
- repeated validation commands;
- conflicting path permissions;
- missing contract fields;
- workflow/task validation disagreement;
- toolchain/manifest contradictions;
- unresolved markers;
- absent context targets;
- excess context;
- audit metadata leaked into model text.

`show-job` reads the immutable historical packet and manifest. It must never
reconstruct an old job with current instructions and present that as the
executed prompt.

Extend `job-context.json` to preserve the complete task:

- `artifact_links`;
- `parent_id`;
- `metadata`;
- generated contract, source task hash, and compiled execution-contract hash;
- source hashes;
- repository base;
- resolved checks.

## Delivery sequence

### Phase 0: characterize the current compiler

Before changing behavior:

1. check in golden prompt fixtures for bootstrap, narrow bug-fix,
   implementation-spec, breakdown, and self-review transitions;
2. encode every defect from `qwen-task-2-prompt-analysis.md` as a failing lint
   or characterization assertion;
3. record current prompt size, duplicate-block count, command consistency, and
   task-to-policy ratio;
4. define a fixed Qwen replay corpus and lock model/runtime settings.

Exit criteria:

- the Task 2 contradictions and duplication are reproducible in tests;
- historical and current-render prompt modes are explicitly distinguished;
- later improvements can be compared against a stable baseline.

### Phase 1: fix direct contradictions and duplication

1. make runtime completion the only owner of the curl block;
2. make runtime execution policy the only owner of failure behavior;
3. shorten `qwen-implementation.agent.md`;
4. replace the blanket Markdown ban;
5. render explicit validation evidence keys;
6. allow no-change self-review;
7. remove language-specific npm defaults from the generic project template and
   require projects to declare compatible checks.

Exit criteria:

- completion and validation commands occur once;
- no scope rule contradicts the task's allowed files;
- generic project creation does not assume Node;
- current runtime and Docker-backed tests remain green.

This phase improves current prompts immediately. Unstructured tasks remain
valid user input; automatic contract preparation arrives in Phase 2.

### Phase 2: automatically normalize any implementation task

1. add the v1 generated-contract parser and profile rules;
2. keep `Todo` as free-form intake and add `ReadyToImplement`;
3. add the high-level `PrepareExecutionContract` transition and instruction;
4. update direction, specification, and breakdown prompts/templates;
5. add invariant declarations to the workflow;
6. migrate the default project template and one fixture project;
7. route existing unstructured implementation tasks through preparation.

Exit criteria:

- a user can create a one-line or arbitrarily structured task without contract
  frontmatter;
- every Qwen job receives a valid generated v1 contract;
- invalid generated artifacts are returned to the high-level model;
- existing tasks are enriched automatically rather than rejected as legacy;
- the high-level model supplies all architecture and interface decisions needed
  by the task.

### Phase 3: compile compact, deterministic Qwen packets

1. add repository facts and baseline manifests;
2. compile and freeze execution contracts at job creation;
3. introduce typed prompt sections and budgets;
4. switch implementation context to explicit excerpts;
5. move audit metadata to the prompt manifest;
6. make preview and execution share the compiler.

Exit criteria:

- an implementation packet follows the target section order;
- full parent/spec documents are absent unless an excerpt is selected;
- task, prompt, and verifier share one contract hash;
- the reconstructed Task 2 packet contains the exact Python work order and no
  npm commands;
- an implementation packet is normally below 6,000 words, with no silent
  truncation.

### Phase 4: enforce the contract and improve convergence

1. derive actual workspace changes from manifests;
2. enforce allowed paths and size budgets;
3. run focused checks and invariants from the frozen contract;
4. classify failures and enforce bounded repair attempts;
5. create the self-review packet;
6. persist implementation diffs and validation evidence;
7. accept a valid no-change review.

Exit criteria:

- Qwen cannot hide or omit an out-of-scope edit;
- Qwen and Tulid see and run the same commands;
- baseline and environment failures do not trigger broad code repair;
- self-review checks the actual implementation instead of restarting it;
- a correct implementation can pass review without gratuitous edits.

### Phase 5: observability and empirical rollout

1. add `prompts explain`, `prompts lint`, and `prompts show-job`;
2. run the fixed replay corpus with old and new prompt compilers;
3. compare outcome metrics;
4. tune section budgets and task-size heuristics from results;
5. migrate existing project workflows while automatically preparing contracts
   for their existing tasks.

Exit criteria:

- prompt defects are visible before model execution;
- immutable historical prompts are inspectable;
- the new compiler materially improves Qwen results under controlled settings;
- legacy projects keep their task contents and receive automatic contract
  preparation plus actionable diagnostics when a real decision is missing.

## Main file touchpoints

Expected existing files:

- `src/open_tulid/templates/default_project/agents/direction-authoring.agent.md`
- `src/open_tulid/templates/default_project/agents/technical-direction-template.agent.md`
- `src/open_tulid/templates/default_project/agents/implementation-spec.agent.md`
- `src/open_tulid/templates/default_project/agents/implementation-spec-template.agent.md`
- `src/open_tulid/templates/default_project/agents/task-breakdown.agent.md`
- `src/open_tulid/templates/default_project/agents/qwen-implementation.agent.md`
- `src/open_tulid/templates/default_project/agents/self-review.agent.md`
- `src/open_tulid/templates/default_project/workflow.yaml`
- `src/open_tulid/domain/schema.py`
- `src/open_tulid/runtime/completion.py`
- `src/open_tulid/runtime/executor.py`
- `src/open_tulid/runtime/instructions.py`
- `src/open_tulid/runtime/context.py`
- `src/open_tulid/runtime/scheduler.py`
- `src/open_tulid/runtime/task_manager.py`
- `src/open_tulid/runtime/workspaces.py`
- `src/open_tulid/runtime/verifier.py`
- `src/open_tulid/cli/main.py`
- workflow language schema/compiler files for project invariants.

Expected new files:

- `src/open_tulid/templates/default_project/agents/execution-contract-authoring.agent.md`
- `src/open_tulid/runtime/task_contracts.py`
- `src/open_tulid/runtime/execution_contracts.py`
- `src/open_tulid/runtime/repository_facts.py`
- `src/open_tulid/runtime/prompts.py`
- focused unit tests for each new module;
- golden prompt and replay fixtures.

## Cross-cutting test matrix

| Area | Required proof |
|---|---|
| User task freedom | One-line, long-form, imported, and arbitrary Markdown tasks enter automatic preparation unchanged |
| Planner output | Valid contracts pass; ambiguous tasks return actionable errors |
| Contract preparation | Generated artifacts link to source hashes; source edits trigger regeneration |
| Readiness | Stale hashes, bad paths, unknown invariants, and toolchain conflicts block scheduling |
| Prompt structure | Correct order, one singleton section each, exact commands, deterministic output |
| Context | Only declared excerpts are included and budgets are enforced |
| Repository facts | Bootstrap and mature repositories are described accurately |
| Scope | Added, edited, removed, and renamed files are derived without `.git` |
| Verification | Frozen focused checks and invariants are independently rerun |
| Repair | Implementation failures are bounded; other failure classes do not consume attempts |
| Self-review | Actual change evidence is supplied; no-op review is accepted |
| History | Executed packets remain immutable and distinct from current previews |
| Compatibility | Planning and non-Qwen transitions continue to work during migration |

Run the normal unit suite after every phase and the Docker-backed runtime tests
after phases that change workspaces, completion, verification, or worker
prompts.

## Evaluation and rollout gates

Use at least these task shapes in the replay corpus:

- empty-repository package bootstrap;
- narrow bug fix in an existing module;
- public-interface change with two callers;
- test-only regression task;
- task preserving an existing end-to-end invariant;
- integration task adding a new invariant;
- correct implementation requiring a no-change self-review;
- impossible or stale contract that must be rejected before Qwen.

Hold constant:

- Qwen model build and quantization;
- context limit and sampling settings;
- repository starting commit;
- container image;
- generated execution contract;
- Tulid version for each comparison arm.

Record:

- first-submission trusted pass rate;
- final pass rate after bounded repair;
- project-invariant pass rate;
- out-of-scope change rate;
- changed-file evidence mismatch rate;
- completion rejection count;
- no-op review rate and gratuitous-review-edit rate;
- prompt words and bytes;
- Qwen wall time and repair attempts;
- human assessment that the result is runnable and matches the task.

Roll out the new implementation prompt only when:

- all structural and contradiction lint checks pass;
- first-submission and final trusted pass rates improve on the replay corpus;
- out-of-scope and gratuitous-review edits decrease;
- no project invariant regresses;
- prompt size falls substantially without losing required contract facts.

## Definition of done

This effort is complete when:

1. the user can create tasks in any useful structure without learning or
   maintaining an execution-contract schema;
2. the high-level model automatically converts implementation intent into a
   versioned, decision-complete generated contract;
3. Tulid rejects missing decisions, stale planning inputs, invalid proposal
   references, and incompatible validation commands before Qwen starts;
4. implementation prompts are compact, task-first, deterministically selected,
   and contain no duplicate completion, validation, or scope policy;
5. Qwen follows one fixed inspect-implement-test-audit-submit loop;
6. prompt rendering and trusted verification consume the same immutable
   execution contract;
7. Tulid enforces actual change boundaries and project invariants;
8. self-review receives the prior change and may correctly produce no patch;
9. prompt changes are evaluated by runnable outcomes rather than prompt style
   alone.

The first three implementation priorities are:

1. add automatic high-level-model contract preparation for arbitrary
   user-authored tasks;
2. freeze one execution contract used by both prompt rendering and validation;
3. replace concatenated prompt text with owned, ordered, singleton sections.

Together, those changes move the system from “ask Qwen to interpret a large
bundle of instructions” to “give Qwen a precise work order and deterministically
prove that it completed it.”
