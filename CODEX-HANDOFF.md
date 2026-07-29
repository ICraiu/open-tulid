# Open Tulid / Qwen Contract Work — Codex Handoff

Updated: 2026-07-29

## Start here

The project is partway through turning Tulid into a deterministic control plane for local Qwen implementation work.

The intended loop is:

1. A user supplies a task and repository.
2. Panalyzer eventually supplies high-level repository context and a proposed change surface.
3. Tulid validates that proposal and freezes an immutable, content-addressed execution contract.
4. Qwen implements only that contract in an isolated workspace.
5. Tulid independently checks the resulting diff, scope, budgets, and behavior.
6. Tulid either promotes the change or starts a bounded repair/planning transition.

The first third of that system is committed. A substantial second slice—repository facts, baseline capture, and immutable job execution contracts—is implemented and tested but **not committed**. Preserve the current working tree.

The next implementation slice is the **structured prompt compiler** described below. Do not jump directly to verifier enforcement: execution and prompt preview first need to consume one compact, deterministic packet compiled from the frozen execution contract.

## Repository state

- Branch: `master`
- `HEAD`: `ba05496` — `implemented 1/3 of the local llm contract`
- At the time of this handoff, `origin/master` also points to `ba05496`.
- The working tree is intentionally dirty. Do not reset, restore, or overwrite it.
- `PLAN-STATUS.md` is the short human-facing status page.
- This file is the implementation handoff for a clean Codex session.

Expected working-tree changes:

```text
 M src/open_tulid/cli/main.py
 M src/open_tulid/runtime/__init__.py
 M src/open_tulid/runtime/completion.py
 M src/open_tulid/runtime/executor.py
 M src/open_tulid/runtime/jobs.py
 M src/open_tulid/runtime/scheduler.py
 M src/open_tulid/runtime/task_manager.py
 M src/open_tulid/runtime/verifier.py
 M src/open_tulid/runtime/workspaces.py
 M tests/runtime/test_jobs_scheduler.py
?? PLAN-STATUS.md
?? src/open_tulid/runtime/execution_contracts.py
?? src/open_tulid/runtime/repository_facts.py
?? tests/runtime/test_execution_contracts.py
?? tests/runtime/test_repository_facts.py
```

Before changing code, run `git status --short` and reconcile it with this list. Any additional changes may belong to the user.

## Source plans and their roles

Read these in order:

1. `improvement-plan.md` — product-level target architecture and rollout phases.
2. `docs/runtime-prompt-architecture-plan.md` — detailed workstreams, data structures, prompt shape, budgets, acceptance criteria, and sequencing.
3. `PLAN-STATUS.md` — concise status against the overall plan.
4. This file — exact repository handoff and next implementation slice.

If the documents differ, preserve the architectural invariants in `improvement-plan.md`, then use the detailed runtime plan for implementation sequencing.

## What is already committed

Commit `ba05496` establishes the generated implementation-contract flow:

- Added the `tulid.implementation/v1` generated contract parser, validation profiles, and diagnostics.
- Added `PrepareExecutionContract` and `ReadyToImplement` workflow states.
- Made source intent hashing ignore workflow state and generated-artifact links, so workflow bookkeeping does not silently change task intent.
- Added stale-contract detection, invalidation, and a recovery event.
- Made generated contract artifacts versioned and content-named.
- Ensured the newest generated contract is selected as task context.
- Improved planning prompts and reduced Qwen implementation instructions.
- Removed hard-coded npm assumptions.
- Reduced completion instructions to one clear submission block.
- Added the valid no-edit `SelfReview` outcome.
- Added prompt-rendering documentation and end-to-end coverage.

This committed layer answers: “What implementation contract did planning generate, and is it valid enough to prepare?”

It does **not** by itself freeze all inputs used by a particular worker job.

## What is implemented but not committed

The current working tree adds the job-specific freeze boundary.

### Repository facts and baseline

`src/open_tulid/runtime/repository_facts.py` now:

- Captures repository commit and dirty state.
- Detects top-level entries, manifests, and declared entry points from files such as `pyproject.toml`, `package.json`, and `Makefile`.
- Builds a deterministic file hash manifest for the worker-visible repository.
- Excludes non-product/runtime-heavy paths such as `.git`, `.open-tulid`, `.venv`, build outputs, caches, and `node_modules`.
- Supports empty and non-git repositories.

This manifest—not worker-reported git output—is intended to become Tulid’s trusted diff authority because the isolated worker workspace does not include `.git`.

### Immutable execution contract

`src/open_tulid/runtime/execution_contracts.py` now:

- Defines the content-addressed `tulid.execution/v1` job contract.
- Freezes the task identity and content, source transition, generated implementation contract, resolved checks, repository facts, and baseline manifest.
- Rejects contradictory check definitions with the same check ID.
- Rejects unsafe shell-control tokens in resolved check commands.
- Resolves legacy transition validations for compatibility.
- Produces deterministic content and SHA-256 identity.
- Reloads contracts with schema and hash-integrity checks rather than silently recompiling them.

The generated `tulid.implementation/v1` contract and job-specific `tulid.execution/v1` contract are intentionally separate:

- The implementation contract is planning output associated with the task.
- The execution contract is a frozen snapshot of exactly what one job is allowed and required to do.

### Job, workspace, execution, and completion integration

The modified runtime files now:

- Compile the execution contract before creating a contract-backed job.
- Require a project root when creating those jobs.
- Pass tracker/repository roots consistently from the scheduler and CLI.
- Store the execution contract and its hash in immutable job metadata.
- Reject attempts to replace that metadata during job status updates.
- Re-capture the copied workspace baseline before starting the worker and reject drift.
- Write these worker artifacts:
  - `.open-tulid/execution-contract.json`
  - `.open-tulid/repository-facts.json`
  - `.open-tulid/baseline-manifest.json`
- Load and hash-check the frozen contract in the executor.
- Use the frozen task and transition instead of recompiling mutable live task state.
- Load the frozen transition during completion and use its expected destination state.
- Pass the execution contract into `DeterministicVerifier`.
- Use the same execution-contract compilation route for implementation/review prompt preview.
- Carry the execution-contract hash in prompt render results.

Legacy jobs that do not use generated implementation contracts remain compatible.

### Current verifier behavior

The verifier currently checks that the transition being verified matches the frozen execution contract.

It does **not yet**:

- Compare the post-worker repository manifest with the frozen baseline.
- Enforce allowed/forbidden paths, file counts, line budgets, deletion rules, rename rules, or generated-file rules.
- Independently run all focused checks frozen in the execution contract.
- Produce a complete machine-readable acceptance report.

Do not describe the current verifier as full contract enforcement.

## Architectural decisions that must remain true

- The user task remains free-form Markdown. Strict machine fields belong in generated internal contracts.
- Source-intent identity excludes workflow state and generated artifact links.
- A prepared execution contract is immutable and content-addressed. Never silently regenerate it when a job starts or completes.
- Repository facts are captured by Tulid, not accepted from Qwen.
- Resolved check IDs must have one unambiguous definition.
- Commands are argv-like data, not arbitrary shell programs.
- Qwen’s completion report is evidence, not proof. Tulid’s independent acceptance result is authoritative.
- A no-change result may validly transition to `SelfReview`.
- Contract-backed and legacy jobs can coexist during migration.
- Product-facing changes will eventually require a vertical-slice check or an explicit exemption.
- There is no planned high-level LLM code-review gate between Qwen and deterministic acceptance.

## Verification already performed

The execution-contract slice was exercised with:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider
```

Recorded results:

- Focused execution-contract/runtime selection: 50 passed.
- Broader runtime selection: 131 passed.
- Full repository suite: 583 passed in 29.65 seconds.
- After the final small readiness/exclusion adjustments, the affected focused selection was rerun: 50 passed.
- `git diff --check` passed.

The final focused run covers the tiny changes made after the full-suite run; a fresh session should still run the full suite before committing the entire slice.

The sandbox may make the default uv cache read-only. If that happens, rerun the same test command with the required execution approval. The repository `.venv/bin/python` does not currently provide pytest directly.

## Exact next slice: structured prompt compiler

Implement Workstream D and the remaining prompt-context parts of Workstream E from `docs/runtime-prompt-architecture-plan.md`.

### Goal

For every contract-backed implementation/review job, compile one deterministic, compact prompt packet from the already frozen `ExecutionContract`. The executor and CLI preview must render the same packet. Audit metadata should be persisted alongside the packet but not waste model context.

Planning prompts can remain on their existing path during this slice.

### Required implementation

1. Add `src/open_tulid/runtime/prompts.py`.
2. Introduce typed prompt structures, at minimum:
   - `PromptSection`
   - a compiled prompt/packet result
   - a prompt manifest containing section identities, hashes, sizes, truncation decisions, and execution-contract hash
3. Render contract-backed prompts in this exact order:
   - Mission
   - Repository Facts
   - Execution Contract
   - Optional Panalyzer Context
   - Selected Context Excerpts
   - Required Validation
   - Execution Procedure
   - Completion Submission
4. Apply deterministic section budgets from the detailed plan:
   - mission/task: 2,500 characters
   - repository facts: 300
   - execution contract: 800
   - Panalyzer context: 2,000
   - completion submission: 500
   - total packet: 6,000
5. Resolve context by explicit artifact and heading, then freeze the selected excerpt and its hash. Do not recursively inject full parent documents.
6. Freeze instruction/context hashes in the execution contract at job creation. Because `tulid.execution/v1` is still uncommitted, its schema can be completed now without a migration, but compiler-version and fixture expectations must be updated consistently.
7. Keep logical labels and model-relevant text in the prompt. Put absolute paths, artifact paths, hashes, byte counts, and truncation/audit details in the prompt manifest.
8. Ensure exactly one completion command and one fenced `curl` example appear in the final packet.
9. Make runtime execution and `tulid prompts render` call the same compiler.
10. Persist the rendered packet and manifest with the job so historical rendering uses frozen content rather than live task/instruction files.

### Acceptance checks for this slice

Add tests proving:

- Section ordering and singleton sections are deterministic.
- Repeated compilation from the same execution contract is byte-identical.
- Runtime execution and CLI preview produce the same packet hash.
- Changing live task or instruction files after job creation does not change the packet.
- Selected heading excerpts are deterministic and bounded.
- Full parent documents are absent from implementation prompts.
- Per-section and total character budgets are enforced deterministically.
- The packet contains one completion command and one fenced `curl` block.
- Absolute paths, hashes, and other audit-only metadata are absent from model text and present in the manifest.
- Legacy/planning prompt behavior remains covered.

Run focused prompt, executor, scheduler, and completion tests first, then the full suite and `git diff --check`.

## Known design gaps after the prompt slice

Complete these in order:

1. **Trusted post-worker enforcement**
   - Capture the post-worker manifest.
   - Calculate add/edit/remove/rename changes from the frozen baseline.
   - Enforce allowed and forbidden surfaces, deletion/generated-file rules, max files, and max changed lines.
   - Run frozen focused and invariant checks without a shell.
   - Persist a structured verification report and failure classification.
2. **Acceptance profiles**
   - Add general profiles for unit, build, static, component, vertical slice, and host smoke.
   - Require task-selected profiles.
   - Require vertical-slice coverage or a recorded exemption for product-facing changes.
3. **Bounded convergence and repair**
   - Classify implementation, contract, environment, and baseline failures.
   - Give Qwen only the authoritative diff/report/evidence needed for repair.
   - Reuse the same workspace and original scope.
   - Cap repair attempts and preserve every attempt/result.
4. **Panalyzer runtime boundary**
   - Require exact-base scans.
   - Validate proposal schema and hashes.
   - Validate task coverage and overlap.
   - Re-scan after accepted tasks and compare the expected architecture delta.
5. **Observability and replay**
   - Add explain/lint/show-job views.
   - Surface prompt manifests and acceptance history.
   - Add golden prompt packets and a replay corpus before broad rollout.

Additional known schema gaps to address as their owning slices are implemented:

- The execution contract does not yet freeze full instruction/context identities and selected-excerpt records.
- Project-owned invariant declarations, allow-listed environment values, and working-directory rules need a complete schema and validation model.
- Self-review supports no-change work, but does not yet receive a compact authoritative prior diff and verification packet.

## Safe continuation checklist

1. Read the four documents listed under “Source plans.”
2. Inspect `git status --short`, `git diff --stat`, and the two new runtime modules.
3. Preserve every existing working-tree change.
4. Run the focused execution-contract tests to establish a local baseline.
5. Implement only the structured prompt slice above.
6. Keep planning/legacy behavior working while switching contract-backed implementation/review jobs.
7. Run focused tests, the full suite, and `git diff --check`.
8. Update `PLAN-STATUS.md` and this handoff when the slice is complete.

The most relevant code entry points are:

- `src/open_tulid/runtime/execution_contracts.py`
- `src/open_tulid/runtime/repository_facts.py`
- `src/open_tulid/runtime/executor.py`
- `src/open_tulid/runtime/task_manager.py`
- `src/open_tulid/runtime/workspaces.py`
- `src/open_tulid/runtime/completion.py`
- `src/open_tulid/runtime/verifier.py`
- `src/open_tulid/cli/main.py`
- `tests/runtime/test_execution_contracts.py`
- `tests/runtime/test_repository_facts.py`
- `tests/runtime/test_jobs_scheduler.py`

