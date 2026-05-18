# Remaining Work Analysis for open-tulid

Date: 2026-05-17  
Inputs reviewed:

- repository at `/home/rawsteel/repo/open-tulid`
- project/spec vault at `/home/rawsteel/repo/obsidian/Agent`
- two independent repository/spec audits, then a comparison pass between the reviewers

## Executive Summary

open-tulid is no longer an architectural sketch. It already contains a real workflow DSL/compiler, an Obsidian adapter with meaningful invariant checks, file-backed jobs/events/journals, resource leases, Docker workers, a completion endpoint, artifact promotion, trusted validation hooks, model-proxy sessions, operator commands, and Docker-backed end-to-end coverage.

The remaining work is therefore not “build the product.” It is the more exacting second half:

```text
make every transition honest,
then make honest transitions survive the real world.
```

The two independent reviews converged on the same broad diagnosis:

1. the project’s **truth boundary is still unevenly enforced**;
2. the runtime is **credible but not yet supervisor-grade**;
3. recovery exists in pieces, but not yet as a **continuous operating discipline**;
4. code promotion, security hardening, operator UX, and docs are still behind the architecture;
5. the tests prove the paved road well, but not enough broken-road behavior.

The most important synthesis point is one neither reviewer wanted lost beneath the runtime backlog:

> open-tulid only becomes a trustworthy system when every state transition—manual or automated—flows through one requirement-validating, transactional path owned by trusted code.

At the time of this analysis, the local suite passes:

```text
463 passed
```

That is a strong foundation. It is not yet the same thing as operational maturity.

## What Is Already Strong

### 1. Workflow language and compiler

This is the cleanest subsystem in the repository.

- standalone DSL frontend with parser, AST, schema, diagnostics, and semantic validation in `src/workflow_engine/*`
- separate compile layer in `src/open_tulid/workflow/*`
- good architectural boundary tests in `tests/workflow/test_boundaries.py`
- broad focused coverage in `tests/workflow_engine/*` and `tests/workflow/*`

The project has already done the difficult early work of separating authored workflow intent from compiled runtime structures.

### 2. Obsidian adapter and file-backed invariants

The adapter is materially stronger than a thin Markdown parser. It enforces:

- ULID identity checks
- duplicate task detection
- duplicate active-board card detection
- board position as canonical state
- missing active card detection

Evidence lives mainly in `src/open_tulid/adapters/obsidian.py`.

### 3. Runtime core

The repo already has serious pieces:

- JSONL structured events and human-readable logs
- transaction journals
- file-backed job persistence
- resource lease persistence and capacity control
- scheduler and execution jobs
- Docker-backed worker launch
- completion endpoint and deterministic verifier
- artifact promotion and compensation/recovery work
- model proxy sessions that keep provider credentials host-side

Core modules:

- `src/open_tulid/runtime/events.py`
- `src/open_tulid/runtime/jobs.py`
- `src/open_tulid/runtime/resources.py`
- `src/open_tulid/runtime/scheduler.py`
- `src/open_tulid/runtime/executor.py`
- `src/open_tulid/runtime/completion.py`
- `src/open_tulid/runtime/verifier.py`
- `src/open_tulid/runtime/model_proxy.py`

### 4. Completion path progress

Several items that older backlog notes described as future work are now implemented:

- payload-size cap and endpoint routing
- replay/idempotency handling
- terminal-job rejection
- completion-token enforcement
- duplicate artifact and changed-file detection
- symlink/path traversal checks
- trusted host-side validation hooks
- changed-file diff comparison when a Git workspace is available
- completion compensation and recovery
- CLI transaction recovery commands

That progress should be preserved in future docs; older assessments now understate the current code.

### 5. Test base

The test suite is not ornamental. It covers:

- workflow loading/compilation
- adapter behavior
- scheduler/resources/jobs
- completion/verifier/transactions
- model proxy
- Docker-backed E2E flows

The next test leap should be about **failure models**, not basic coverage volume.

## The Two Audits: Agreement and Useful Disagreement

Both reviewers agreed that the largest remaining families are:

- runtime supervision
- crash recovery
- scheduler durability
- workspace/Git promotion
- security/isolation hardening
- docs/operator polish
- adversarial tests

They diverged usefully on emphasis:

- One review foregrounded **runtime adulthood**: live supervision, stale detection, token leakage, durable logs, and operator control.
- The other foregrounded **semantic adulthood**: manual transitions bypassing the command/effect boundary, state requirements not being universally enforced, and validation split-brain.

After comparing notes, the joint view is:

```text
P0  unify the truth path
P1  make that path durable under real failure
P2  make the product legible, hardened, and pleasant to operate
```

## Priority Map

```text
P0  Truth boundary
    - one lawful transition path
    - authoritative requirements
    - one authoritative validation path

P1  Operational resilience
    - managed supervision
    - systemic recovery
    - scheduler durability
    - workspace / Git promotion

P1  Trust boundary
    - token hygiene
    - session expiry / scope
    - isolation controls

P2  Product finish
    - CLI/API/spec coherence
    - docs and install readiness
    - adversarial/failure-mode tests
```

# P0 — Make Workflow Truth Actually Authoritative

## 1. Manual transitions still bypass the intended architecture

### Spec intent

The foundational boundary in the specs is:

```text
Interface
-> Task Manager Runtime
-> Workflow Engine
-> Command Result
-> approved Effects
-> Storage Adapter
```

Relevant specs:

- `spec/03-interfaces.md`
- `spec/09-command-result-model.md`
- `spec/11-atomic-transition-plan.md`
- `spec/29-cli-contract.md`

### Current implementation

`TaskManager.request_transition()` validates basic transition shape, but returns no move effect and does not itself perform the transition mutation. The CLI `transition` command then directly calls `adapter.move_task()`.

Evidence:

- `src/open_tulid/runtime/task_manager.py`
- `src/open_tulid/cli/main.py`

### Why this matters

That creates a privileged side road around the project’s own architecture:

- no single transaction path for all transitions
- no uniform requirement enforcement
- no single place to attach recovery logic
- CLI code can mutate trusted state directly

This is the clearest remaining violation of the project’s central promise.

### Required work

- Make manual and automated transitions use the same command/result/effect path.
- Have trusted code produce an explicit `MoveKanbanCard`-style effect for accepted manual transitions.
- Apply that effect through the transaction runtime, not ad hoc CLI mutation.
- Keep CLI as orchestration/presentation only.

### Acceptance target

There should be exactly one lawful route by which canonical task state changes.

## 2. Requirements are not yet authoritative across all paths

### Spec intent

The specs say:

- transition requirements are authoritative;
- a task may not enter a state unless state requirements pass;
- proof/evidence requirements belong to trusted validation, not operator convention.

See especially `spec/24-core-invariants.md`.

### Current implementation

The runtime now preserves and checks more requirement data than before, especially on the completion path. But the first-slice/manual path still checks mainly:

- task exists
- transition exists
- task type matches
- source state matches

`_validate_snapshot_against_workflow()` currently checks unknown task types/states, not the full state requirement model.

Evidence:

- `src/open_tulid/runtime/task_manager.py`
- `src/open_tulid/runtime/verifier.py`

### Required work

- Add one shared requirement evaluator.
- Enforce target-state requirements for all transitions.
- Enforce transition proof/evidence requirements in manual paths too.
- Make project validation evaluate workflow-driven state invariants, not only parse shape.

### Acceptance target

If the DSL says a transition or target state requires something, no code path may enter the next state without trusted proof that the requirement is satisfied.

## 3. Validation is split across two worlds

### Current implementation

There is a strong adapter/runtime validation path and a weaker public `vault validate` path.

- `vault.validator.validate_project()` currently focuses on directory shape and Kanban syntax.
- stronger invariants live separately in the adapter/runtime layer.
- the compiled workflow definition is threaded through `validate_project()` but not materially used.

Evidence:

- `src/open_tulid/vault/validator.py`
- `src/open_tulid/adapters/obsidian.py`
- `src/open_tulid/runtime/task_manager.py`

### Why this matters

The public command named `validate` can give an operator less truth than the runtime actually depends on. That is a dangerous semantic split.

### Required work

- Decide the authoritative validation pipeline.
- Make `tulid validate` call the same workflow-aware validation truth the runtime uses, or rename the current lighter validator so it does not imply more than it proves.
- Add parity tests so public validation and runtime validation cannot drift silently.

### Acceptance target

When an operator sees “valid,” it should mean valid by the same invariants the runtime will rely on later.

## 4. Canonicalize the spec era

The docs currently contain two overlapping workflow stories:

- older flow-schema language using `states`, `transitions`, and `storage.obsidian`
- newer DSL/compiler language using `statements`

The implementation follows the newer DSL. That may be the right path, but the documentation needs to say so unambiguously.

### Required work

- Mark superseded specs as historical or rewrite them to the current DSL.
- Separate:
  - current contract
  - historical design note
  - completed review brief
  - obsolete material
- Make the README and spec index point to one canonical present-day workflow model.

# P1 — Make the Runtime Survive Reality

## 5. Runtime supervision is still blocking rather than managed

### Spec intent

The supervision notes require:

- durable container identity
- live stdout/stderr streaming
- wrapper heartbeat
- startup/heartbeat/idle/total/shutdown timeout classes
- stale-runtime detection
- inspect/tail/stop/kill operator controls
- crash reconciliation after host failure

Relevant docs:

- `runtime-supervision.md`
- `runtime-remaining-work-plan-2026-05-15.md`

### Current implementation

The executor still blocks on a `docker run`-style call and writes captured logs after process exit.

Evidence:

- `src/open_tulid/containers/runtime.py`
- `src/open_tulid/runtime/executor.py`

### Required work

- Replace blocking execution with managed supervision.
- Persist container ID, image identity, start time, supervisor PID, and endpoint metadata on jobs.
- Stream worker logs live into durable storage outside disposable workspaces.
- Add independent heartbeat state.
- Implement separate timeout classes.
- Add live operator controls:
  - inspect
  - tail/follow
  - stop
  - kill
- Detect:
  - running job without container
  - container without matching job
  - dead completion endpoint
  - stale heartbeat
  - worker exit before accepted completion

### Acceptance target

One worker attempt should be observable, controllable, and recoverable while it is still running.

## 6. Recovery exists, but not yet as a living system

### Current strength

The repo now has:

- transaction journals
- compensation support
- completion recovery
- job-creation recovery
- CLI transaction recovery commands

### Remaining gap

Recovery is still mostly something a human invokes after noticing damage. It is not yet a continuous runtime discipline.

Missing or weak:

- startup reconciliation
- host-restart detection
- stale endpoint detection
- stale lease reconciliation when owner files still exist but the runtime is dead
- broad stale-lock policy
- recovery-state visibility in normal operator flows

### Required work

- Add startup reconciliation across jobs, leases, containers, endpoints, and journals.
- Distinguish prepared, failed, recoverable, orphaned, and operator-required states.
- Add recovery tests at every effect boundary.
- Surface unresolved recovery conditions in validation and status commands.

## 7. Scheduler durability is still early

### Current strengths

- active-job checks exist
- resource capacity leases exist
- scheduler can create jobs transactionally
- a daemon mode exists

### Remaining gaps

- no broad project-level lock around `load -> select -> create`
- no retry/backoff policy
- no richer scheduling modes such as `--limit`, `--all`, `--until-empty`
- scheduler skips/retries are not yet a rich structured event story
- stale-job semantics need review against the spec
- lease cleanup does not yet cover all dead-runtime cases
- detached runtime/process state is not yet robust enough for unattended use

Evidence:

- `src/open_tulid/runtime/scheduler.py`
- `src/open_tulid/runtime/jobs.py`
- `src/open_tulid/runtime/resources.py`
- `src/open_tulid/cli/main.py`

### Required work

- Add scheduler locking around the whole decision path.
- Formalize active/stale semantics.
- Add retry policy:
  - max attempts
  - retryable failure classes
  - exponential backoff
  - separate treatment for completion rejection vs worker failure
- Add structured scheduler events.
- Add configurable global and per-project concurrency.
- Reconcile stale jobs and leases automatically.

## 8. Workspace / Git promotion model is unresolved

### Current implementation

- workspaces are copied snapshots
- `.git` is excluded from the copied workspace
- artifact promotion exists
- code promotion does not yet have one complete trusted route into the canonical repo

Evidence:

- `src/open_tulid/runtime/workspaces.py`
- `src/open_tulid/runtime/verifier.py`
- `src/open_tulid/runtime/completion.py`

### Why this matters

A worker can produce accepted implementation work whose code remains stranded inside a disposable workspace. That is not merely a missing flourish; it leaves the product incomplete as a software-delivery system.

### Required work

Choose one product model and finish it:

1. disposable workspace + trusted patch promotion, or
2. branch-based workspace + trusted Git brokerage.

Then add:

- dirty-tree checks
- diff capture
- trusted changed-file verification
- branch/patch naming rules
- cleanup rules
- inspectable promotion records

### Acceptance target

Accepted code must have a defined, auditable route into the real repository.

## 9. Transactions are improved, but not yet fully resistant to concurrent edits

### Current strengths

- prepared journals
- ordered effects
- final validation hook
- compensation support
- adapter temp writes

### Remaining gaps

- no project/task revision hashes before mutation
- no complete lost-update protection against human edits between read and write
- no broad stale-lock policy
- adapter durability semantics still need a hard pass around fsync/revision guarantees
- manual transition path currently bypasses the strongest machinery

### Required work

- Add revision checks before mutation.
- Validate final trusted state against the revision read.
- Define stale-lock handling.
- Make compensation/recovery policy explicit for every effect class.

# P1 — Tighten the Trust Boundary

## 10. Scoped credentials still leak into durable logs

The architecture has a good direction: long-lived provider secrets remain host-side and workers receive scoped sessions/tokens. But command logs currently serialize full Docker commands including scoped tokens.

Evidence:

- `src/open_tulid/runtime/executor.py`
- `src/open_tulid/containers/runtime.py`
- E2E output shows `OPEN_TULID_COMPLETION_TOKEN=...` inside persisted `command.txt`

### Required work

- Redact token-bearing env values from command logs.
- Add regression tests that prove secrets/tokens never persist in command/event/human logs.
- Treat scoped tokens as sensitive even if they are not long-lived provider credentials.

## 11. Isolation is weaker than the architecture wants to claim

Current container launch lacks an explicit policy for:

- network mode / allowlist
- non-root user
- read-only root filesystem
- CPU / memory / PID limits
- capability minimization
- no-new-privileges posture

Evidence:

- `src/open_tulid/containers/runtime.py`
- `src/open_tulid/containers/agents/*.Dockerfile`

### Required work

- Be explicit about which guarantees apply to `trusted-local` vs `isolated-container`.
- Add actual Docker hardening controls before making strong isolation claims.
- Add tests for configured isolation policy.

## 12. Model proxy/session policy still needs hardening

Current strengths:

- worker containers get scoped proxy sessions, not provider keys
- proxy traffic can be gated by leases

Remaining work:

- expiry/revocation policy for sessions
- route/method allowlisting
- transcript redaction
- safer body logging defaults and retention
- auditing of proxy access decisions

Evidence:

- `src/open_tulid/runtime/model_proxy.py`

# P2 — Product Finish

## 13. CLI contract and API shape have drifted

Examples:

- the spec promises more JSON support than the CLI currently exposes
- `--project`, task filtering, and proof handling are not yet consistently productized
- completion API behavior differs in some places from the richer written contract
- command vocabulary has grown faster than the docs

Evidence:

- `spec/29-cli-contract.md`
- `spec/18-completion-endpoint.md`
- `src/open_tulid/cli/main.py`
- `src/open_tulid/runtime/completion_http.py`

### Required work

- Decide the current canonical CLI/API contract.
- Add JSON output parity where automation needs it.
- Finish proof handling for manual transitions.
- Align accepted completion responses and evidence shapes with the decided contract.

## 14. README and internal docs are stale relative to the code

The README still describes a much smaller product than the actual CLI now provides. Some internal docs are implementation notes or historical review artifacts that read like present-tense contracts.

### Required work

- Rewrite README as an operator guide for the real system.
- Add a current architecture overview.
- Mark historical docs clearly.
- Document:
  - setup
  - runtime modes
  - supervision model
  - failure/recovery playbooks
  - security guarantees and non-guarantees
  - what validation actually proves

## 15. Install and packaging readiness remain prototype-grade

Remaining polish:

- editable-install-centric setup
- relatively loose dependency reproducibility
- agent image builds that still rely on moving upstream versions
- no strong built-wheel smoke path
- incomplete readiness validation for images, proxies, endpoints, permissions, and runtime config

### Required work

- add runtime readiness checks
- add wheel-install smoke testing
- make worker images reproducible
- document supported install paths

# P2 — Test the Broken Roads

The next test frontier should be failure-oriented.

## Highest-value additions

1. manual transition goes through the lawful effect path
2. state requirements block invalid entry
3. public validation and runtime validation stay in parity
4. concurrent scheduler race on the same task
5. host restart while a worker is running
6. running job with missing container
7. orphan container with missing/mismatched job
8. stale lease with living owner file but dead runtime
9. interruption after each transaction boundary
10. human edit between adapter read and write
11. token absence from persisted logs
12. transcript redaction
13. workspace rerun contamination
14. Git promotion/diff correctness once the product model is chosen
15. real wheel-install smoke test

## Test philosophy

The current suite proves the system can work. The next suite should prove it refuses to lie when reality is untidy.

# Joint Top-10 Remaining-Work List

After comparing both independent reviews, this is the backlog both reviewers would endorse:

1. **Restore one trusted transition path** for manual and automated work.
2. **Make requirements truly authoritative** everywhere transitions or validation occur.
3. **Unify validation** so public validation and runtime truth cannot diverge.
4. **Replace blocking execution with managed supervision** and durable live logs.
5. **Turn recovery into a continuous operational system**, not only repair commands.
6. **Make scheduler semantics durable**: locks, retries, stale handling, concurrency, structured events.
7. **Choose and finish the workspace/Git promotion model.**
8. **Close concrete security gaps first**: token redaction, session expiry, transcript hygiene, then stronger isolation.
9. **Bring CLI/API/docs/install behavior into one current product contract.**
10. **Expand adversarial and interruption-focused tests.**

# Recommended Execution Order

```text
1. Unify transition execution + requirement enforcement + validate
2. Remove credential leakage from durable logs
3. Build managed runtime supervision
4. Add startup/runtime reconciliation and scheduler durability
5. Choose and implement the Git/promotion model
6. Finish revision-aware mutation safety
7. Harden isolation/proxy policy
8. Reconcile CLI/API/docs/install flows
9. Add the broken-road test matrix
```

Why this order:

- Step 1 protects correctness.
- Step 2 closes an already concrete security defect.
- Steps 3–6 turn the runtime from impressive to trustworthy.
- Steps 7–9 make the product honest, operable, and resilient at scale.

# Final Assessment

open-tulid already has several unusually good architectural instincts:

- agents do not own process truth
- scarce resources are separate from worker identity
- provider credentials belong behind a host-side broker
- file-backed work still deserves journals, compensation, and audit records

The project is close to a more interesting threshold than “feature complete.” It is close to becoming a system whose claims are actually enforced by its shape.

The remaining work is not glamorous scaffolding. It is the work that turns a capable prototype into something calm under pressure:

```text
one truth path
durable supervision
recoverable failure
honest security claims
clear operator surfaces
```

That is the road from clever to solid.
