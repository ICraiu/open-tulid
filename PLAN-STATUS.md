# Qwen Improvement Plan: Status and Roadmap

**Updated:** 2026-07-27  
**Baseline:** commit `ba05496` — “implemented 1/3 of the local llm contract”  
**Current position:** contract preparation and immutable job contracts work; post-worker acceptance enforcement is next.

## Destination

The plan replaces “give Qwen a large prompt and trust its report” with a closed, testable loop:

```text
User task + repository + Panalyzer
→ high-level model creates a bounded work order
→ Tulid validates and freezes it
→ Qwen implements
→ Tulid independently checks the real diff and runnable behavior
→ pass: promote | fail: bounded repair or return to planning
```

Users keep writing tasks in any useful format. The high-level model owns design decisions; Qwen owns scoped implementation; Tulid owns acceptance.

## The five-part plan

### 1. Make Panalyzer a runtime boundary — Not started

Run a full scan against the exact base commit, validate the proposal, and store scan/proposal hashes. Assign every proposed file, symbol, and reference edge to exactly one task. After implementation, re-scan and reject structural changes outside that assignment. Use this data to serialize overlapping tasks and safely parallelize independent ones.

**Today:** Panalyzer information is planning/prompt context; Tulid does not enforce proposal coverage or before/after structural deltas.

### 2. Give Qwen an executable work order — Core delivered

Each task needs one observable objective, exact interfaces and behaviors, allowed/forbidden paths, prerequisites, focused checks, project invariants, budgets, proposal coverage, base commit, and repair limit.

**Done now:** free-form tasks pass through `PrepareExecutionContract`; the `tulid.implementation/v1` artifact is schema-validated, bound to task ID and intent hash, versioned, injected into Qwen context, and regenerated when stale.

The next slice now compiles a content-addressed `tulid.execution/v1` contract at job creation. It freezes the source task, transition requirements, generated contract, resolved checks, repository commit/dirty state, detected manifests and entrypoints, and a hash manifest of every baseline file. The same frozen task and transition are used by execution, prompt preview, and verification. Corrupt metadata or repository drift before execution is rejected; exact snapshots are written under `.open-tulid/`.

**Remaining:** add Panalyzer identities, proposal coverage, path/symbol budgets, deterministic context excerpts, and instruction/context hashes.

### 3. Add reusable runnable acceptance profiles — Not implemented

Projects should declare named `unit`, `build`, `static`, `component`, `vertical_slice`, and optional `host_smoke` profiles. A profile defines command arguments, timeout, allowed environment, fixtures, readiness, action, and observable assertions. Product-facing tasks require a deterministic vertical slice or an explicit exemption.

**Today:** contracts can declare focused command arrays and invariant IDs, but there is no general profile system or contract-driven vertical-slice runner.

### 4. Add bounded repair and real verification — Partially implemented

Tulid should classify failures as implementation, contract, environment, or baseline failures. Only implementation failures return to Qwen, in the same workspace, with a compact diagnostic packet and at most two targeted repairs. Verification must allow an empty diff when the work is already correct.

**Done now:** no-change self-review is accepted.  
**Remaining:** failure classification, repair packets, attempt limits, and a distinct no-edit verification transition using prior diff and test evidence.

### 5. Enforce boundaries and promote only proven work — Foundation started

Capture a pre-worker file manifest, derive the authoritative diff, enforce add/edit/remove rules, allowed paths and symbols, generated-file policy, file/line budgets, and all selected checks. Record the base commit, diff hash, check results, Panalyzer delta, and repair history. If the repository advanced, replay into a clean workspace and rerun acceptance before promotion.

**Today:** the trusted baseline manifest and frozen transition checks now exist, and repository drift between scheduling and execution is blocked. Existing transition validations still run from the frozen transition. The verifier does not yet compare the post-worker tree to the baseline or enforce the generated contract’s paths, budgets, focused checks, or proposal surface.

## Delivery order from here

1. **Freeze the contract — Done:** repository facts, baseline manifest, resolved checks, integrity hashes, immutable job metadata, and workspace snapshots.
2. **Compile compact prompts — Next:** ordered singleton sections, explicit excerpts, size budgets, and frozen instruction/context hashes.
3. **Enforce acceptance:** trusted post-worker diff, scope/budget checks, focused and vertical-slice profiles.
4. **Converge safely:** failure classification, bounded repair, evidence-based self-review, stale-base replay.
5. **Close the loop:** Panalyzer deltas, conflict-aware scheduling, prompt lint/explain/history, and controlled Qwen replay metrics.

## Latest-commit proof

The contract lifecycle, immutable compilation, repository facts, baseline drift detection, corruption checks, prompt preview, artifact handling, and no-change self-review have unit/runtime/E2E coverage. **All 583 repository tests pass.**

**Bottom line:** Tulid now creates and freezes the exact work order before Qwen starts. The next milestone is a compact prompt compiled from that frozen object; the following milestone makes Tulid independently prove the implementation stayed inside it.
