# Qwen Improvement Plan: Status and Roadmap

**Updated:** 2026-07-29
**Baseline:** commit `d4ed9e4` — “improved qwen prompt structure”
**Current position:** contract preparation, immutable job contracts, compact frozen implementation and self-review prompt compilation, trusted post-worker enforcement, bounded repair, reusable frozen acceptance profiles, and prompt observability are implemented.

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

**Remaining:** add the Panalyzer runtime boundary, proposal coverage, and structural path/symbol enforcement. Deterministic context excerpts, prompt policy identities, section hashes, prompt manifests, path budgets, and change budgets are implemented.

### 3. Add reusable runnable acceptance profiles — Foundation implemented

Projects should declare named `unit`, `build`, `static`, `component`, `vertical_slice`, and optional `host_smoke` profiles. A profile defines command arguments, timeout, allowed environment, fixtures, readiness, action, and observable assertions. Product-facing tasks require a deterministic vertical slice or an explicit exemption.

**Done now:** projects may declare `acceptance.yaml` profiles of type unit, build, static, component, vertical_slice, or host_smoke. Generated contracts select them by ID; Tulid validates, freezes, independently runs, and reports their argv checks.

**Remaining:** fixture/readiness/action/assertion lifecycle and capability-gated host smoke. The default template now requires every product-facing contract to select a `vertical_slice` profile or record a concrete exemption.

### 4. Add bounded repair and real verification — Implemented

Tulid should classify failures as implementation, contract, environment, or baseline failures. Only implementation failures return to Qwen, in the same workspace, with a compact diagnostic packet and at most two targeted repairs. Verification must allow an empty diff when the work is already correct.

**Done now:** deterministic failure classification, evidence-only repair packets, attempt limits, same-workspace repair, and no-change self-review are implemented. Self-review now receives a distinct frozen packet containing the accepted implementation job's authoritative change summary, trusted check results, and repair history; it is not scheduled without that evidence.

### 5. Enforce boundaries and promote only proven work — Foundation started

Capture a pre-worker file manifest, derive the authoritative diff, enforce add/edit/remove rules, allowed paths and symbols, generated-file policy, file/line budgets, and all selected checks. Record the base commit, diff hash, check results, Panalyzer delta, and repair history. If the repository advanced, replay into a clean workspace and rerun acceptance before promotion.

**Done now:** Tulid derives add/edit/remove/rename changes from the frozen baseline manifest, enforces generated-contract add/edit/forbidden surfaces plus optional file and changed-line budgets, runs frozen argv checks without a shell, and persists a machine-readable verification report with deterministic failure classification. Legacy jobs retain their existing verifier behavior.

## Delivery order from here

1. **Freeze the contract — Done:** repository facts, baseline manifest, resolved checks, integrity hashes, immutable job metadata, and workspace snapshots.
2. **Compile compact prompts — Implemented and verified:** ordered singleton sections, explicit frozen heading excerpts, size budgets, and persisted packet manifests.
3. **Enforce acceptance — In progress:** trusted post-worker diff, scope/budget checks, frozen focused checks, reusable command profiles, and vertical-slice policy are complete; profile lifecycle orchestration remains.
4. **Converge safely — In progress:** implementation failures resume as bounded, evidence-only repairs and self-review audits the prior accepted implementation; stale-base replay remains.
5. **Close the loop:** prompt lint/explain/immutable history are complete. Panalyzer deltas, conflict-aware scheduling, and controlled Qwen replay metrics remain.

## Current working-tree proof

The contract lifecycle, immutable implementation and review prompt compilation, repository facts, baseline drift detection, packet/section corruption checks, prompt preview/inspection/history, artifact handling, vertical-slice policy, and no-change self-review have unit/runtime/E2E coverage. **All 608 repository tests pass.**

**Bottom line:** Tulid now gives Qwen a compact frozen implementation work order, independently proves its result, and gives self-review only the prior authoritative evidence needed for a targeted correction or valid no-op. The remaining roadmap is runtime architecture enforcement and empirical rollout, not prompt assembly.
