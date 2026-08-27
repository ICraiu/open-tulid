# Open Tulid / Qwen Contract Work — Codex Handoff

Updated: 2026-07-29 — post acceptance-profile foundation

## Start here

The project is partway through turning Tulid into a deterministic control plane for local Qwen implementation work.

The intended loop is:

1. A user supplies a task and repository.
2. Panalyzer eventually supplies high-level repository context and a proposed change surface.
3. Tulid validates that proposal and freezes an immutable, content-addressed execution contract.
4. Qwen implements only that contract in an isolated workspace.
5. Tulid independently checks the resulting diff, scope, budgets, and behavior.
6. Tulid either promotes the change or starts a bounded repair/planning transition.

The first third of that system is committed. The working tree additionally contains the repository-facts/execution-contract slice and the structured prompt compiler. Both are uncommitted; preserve the working tree.

The working tree now also contains the bounded-repair slice. Prompt compilation, trusted enforcement, selected acceptance profiles, and repairs consume the same frozen execution contract.

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

The verifier checks that the transition being verified matches the frozen execution contract, captures the post-worker manifest, and derives add/edit/remove/rename changes from the frozen baseline. It enforces allowed add/edit paths, forbidden paths, no-delete/no-rename policy, and optional `max_files` and `max_changed_lines` contract budgets. It runs frozen command checks as argv data and emits a `tulid.verification/v1` report with check output and baseline/environment/contract/implementation classification. Completion persists that report in job metadata and its validation-finished event.

It does **not yet**:

- Support generated-file rules beyond the ordinary allowed-path boundary.
- Run reusable acceptance profiles or deterministic vertical slices.

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
- Projects that enable the product-facing policy require a vertical-slice check or an explicit exemption.
- There is no planned high-level LLM code-review gate between Qwen and deterministic acceptance.

## Verification already performed

The execution-contract slice was exercised with:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider
```

Latest working-tree results:

- Prompt/contract/scheduler/executor/CLI focused selection: 106 passed.
- Contract/scheduler/completion-verifier selection: 68 passed.
- Full repository suite: 608 passed in 29.21 seconds.
- `python -m compileall -q src` and `git diff --check` passed.

The sandbox may make the default uv cache read-only. If that happens, rerun the same test command with the required execution approval. The repository `.venv/bin/python` does not currently provide pytest directly.

## Structured prompt compiler and observability: implemented and verified

`src/open_tulid/runtime/prompts.py` now compiles deterministic, contract-backed implementation and self-review packets with a 6,000-character total limit. It carries a manifest with packet type, section provenance, selection reasons, hashes, sizes, budgets, truncation decisions, packet hash, and execution-contract hash. Binding scope, interface, validation, and completion sections fail closed instead of being truncated. Model text excludes audit hashes and absolute paths.

The implementation contract accepts explicit `context_excerpts` (`artifact`, Markdown `heading`, and selection `reason`). Contract compilation resolves one linked artifact, rejects missing or duplicate headings, extracts through the next equal-or-higher heading, enforces per-excerpt and total budgets, and hashes and freezes the result in `tulid.execution/v1`. It does not recursively inject parents or source documents.

At job creation, Tulid persists the packet, packet hash, and manifest as immutable metadata. Runtime validates and uses that stored packet; CLI preview compiles from the same frozen execution contract. Legacy and planning prompts retain the prior path.

Exact preview-versus-runtime packet equality, mutation isolation, explicit-excerpt bounds, persisted packet/section corruption, and deterministic compilation have focused coverage. `tulid prompts explain`, `lint`, and `show-job` expose live provenance and immutable historical packets without reconstructing old jobs from current inputs.

Self-review is now a distinct packet type. Job creation locates the accepted implementation job that produced the review state and freezes its authoritative change summary, trusted check results, and repair history. Missing evidence blocks scheduling. The Docker-backed no-change review workflow covers this path.

## Acceptance-profile foundation

The profile foundation is implemented: `acceptance.yaml` declares project-owned `unit`, `build`, `static`, `component`, `vertical_slice`, and `host_smoke` argv profiles. Task contracts select them in `checks.profiles`; Tulid validates and freezes them as resolved checks, runs them without a shell, and includes them in the verification report.

Remaining profile work is fixture/readiness/action/assertion orchestration and host-capability gating. The default project enables a product-facing vertical-slice-or-exemption policy, which is validated and frozen into the execution contract.

## Bounded convergence and repair: implemented

Implementation failures now create a bounded `tulid.repair/v1` packet containing only the authoritative verification report, normalized failed-check evidence, error codes, and current diff summary. The scheduler resumes the existing job in its existing workspace; repair execution keeps the frozen contract and never recopies the repository over the worker's diff.

`runtime.max_repair_attempts` defaults to two. Every rejected submission retains its classification, report, error codes, and repair disposition in `repair_history`. Contract, environment, and baseline failures remain rejected and do not consume a repair attempt.

The next run should execute focused repair/completion/scheduler/executor tests, then the complete suite and `git diff --check` before committing the combined slice.

## Known design gaps after the completed prompt work

1. **Panalyzer runtime boundary**
   - Require exact-base scans.
   - Validate proposal schema and hashes.
   - Validate task coverage and overlap.
   - Re-scan after accepted tasks and compare the expected architecture delta.
2. **Acceptance-profile orchestration**
   - Add fixture, readiness, action, and assertion lifecycle support.
   - Add capability-gated host-smoke execution.
3. **Stale-base promotion**
   - Replay accepted work into a clean current-base workspace.
   - Rerun frozen acceptance before promotion.
4. **Empirical replay**
   - Run a fixed corpus against the configured Qwen build and record first-pass/final acceptance, scope violations, prompt size, wall time, and repair attempts.
   - Tune budgets from controlled outcomes. Prompt explain/lint/show-job and immutable manifests are already available.

Additional known schema gaps to address as their owning slices are implemented:

- Project-owned invariant declarations, allow-listed environment values, and working-directory rules need a complete schema and validation model.
- Controlled Qwen replay metrics require a configured model/runtime and remain an operational rollout gate.

## Safe continuation checklist

1. Read the four documents listed under “Source plans.”
2. Inspect `git status --short`, `git diff --stat`, and the prompt/contract runtime modules.
3. Preserve every existing working-tree change.
4. Run the focused execution-contract and prompt tests to establish a local baseline.
5. Select one remaining architecture slice above.
6. Keep contract-backed and legacy planning behavior compatible.
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
