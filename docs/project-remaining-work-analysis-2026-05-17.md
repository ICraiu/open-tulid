# Remaining Work Analysis for open-tulid

Date: 2026-05-17

## Executive Summary

open-tulid has crossed the line from architectural sketch into credible prototype. The repository already contains a real workflow compiler, Obsidian adapter, event journal, scheduler, resource leases, execution jobs, Docker-backed workers, completion endpoint, artifact promotion, model proxy, operator commands, and a meaningful Docker-backed end-to-end path. The full suite currently passes (`451 passed`).

The remaining work is no longer broad scaffolding. It is the harder second half of the project: making the system trustworthy under concurrency, crashes, hostile inputs, long runtimes, and ordinary operator use.

The two independent reviews agreed on the same large gaps:

1. completion acceptance is still too trusting of worker claims;
2. transaction and recovery semantics are incomplete;
3. the security/isolation story is ahead of the implementation;
4. runtime supervision is not yet strong enough for unattended operation;
5. the product surface and docs are behind the architecture;
6. the tests prove the paved road, but not enough of the broken-road cases.

They disagreed mainly on sequencing. One reviewer put **runtime supervision** first because unattended execution is the most visible operational weakness. The other put **trusted verification** first because without it the system can move work forward on assertions that trusted code never actually checked. My synthesis is:

- **first harden what becomes true**: verification + completion transaction safety;
- **then harden how long-running work survives**: supervision + recovery;
- **then tighten isolation, product polish, and adversarial coverage around those stronger cores**.

The project’s deepest promise is that agents may act, but trusted code decides what becomes true. The remaining roadmap should protect that promise above all else.

## Current State: What Is Already Solid

The project already has several well-shaped foundations:

- **Workflow language and compiler**: the DSL, loader, validation, registry, and compiled workflow runtime are implemented and heavily tested (`src/workflow_engine/*`, `src/open_tulid/workflow/*`, `tests/workflow_engine/*`, `tests/workflow/*`).
- **File-backed project model**: Obsidian integration, project validation, domain artifact validation, and Kanban/task parsing exist (`src/open_tulid/adapters/obsidian.py`, `src/open_tulid/vault/*`, `src/open_tulid/domain/*`).
- **Structured runtime records**: JSONL events, human logs, transaction journals, job persistence, and resource lease persistence are present (`src/open_tulid/runtime/events.py`, `jobs.py`, `resources.py`).
- **Agent-runtime golden path**: scheduling, execution jobs, Docker worker invocation, completion endpoint, artifact promotion, and review transition all exist (`src/open_tulid/runtime/scheduler.py`, `executor.py`, `completion.py`).
- **Credential direction**: a host-side model proxy with scoped sessions is already implemented, which is a strong architectural move (`src/open_tulid/runtime/model_proxy.py`).
- **Meaningful test base**: the repository has broad unit coverage and Docker-backed end-to-end scenarios for success, retry-after-rejection, and no-completion failure (`tests/e2e/test_docker_mock_agent_runtime.py`).

This is important context: the right roadmap is not “build the product.” It is “finish the product so the present architecture becomes dependable.”

## Priority Map

```text
P0  Truth and durability
    - trusted verification
    - completion transaction recovery

P1  Unattended operation
    - managed runtime supervision
    - scheduler durability / retries / reconciliation

P1  Trust boundaries
    - isolation controls
    - secret hygiene
    - workspace / git promotion model

P2  Product finish
    - CLI contract parity
    - operator ergonomics
    - docs and install flow

P2  Proof under stress
    - adversarial and failure-mode tests
```

## 1. Trusted Verification Is Not Yet Truly Trusted

### Spec intent

The specs are explicit: transition requirements are authoritative, agents submit evidence, and trusted code decides whether work may move forward (`spec/19-completion-verifier.md`, `spec/24-core-invariants.md`, `spec/25-golden-path-todo-to-code-review.md`, `runtime-remaining-work-plan-2026-05-15.md` Phase 3).

### Current implementation

`DeterministicVerifier` currently checks:

- required artifact types are present;
- artifact paths stay inside the output directory;
- artifacts exist, are non-empty, and optionally match a hash;
- required validation-evidence keys are present and non-empty strings;
- listed changed files exist in the workspace.

See `src/open_tulid/runtime/verifier.py`.

### Why this is insufficient

The verifier does **not** yet:

- run host-side trusted validations such as tests, lint, or builds;
- prove that `validation_evidence` corresponds to a command actually executed by trusted code;
- verify `must_pass` semantics against actual command results;
- require changed-file evidence when the flow says it is required;
- compare `changed_files` against an actual diff;
- reject duplicate artifacts or duplicate changed-file entries;
- validate required markdown structure/fields inside promoted artifacts;
- persist accepted and rejected completion snapshots with redaction.

A worker can currently submit `tests_pass = "passed"` and satisfy the verifier without the host executing tests. That is a philosophical break in the system: the worker is still telling Tulid what is true.

### Hidden schema drift

The flow spec models richer requirements than the domain/runtime currently retain. In particular, `changed_files.required` exists in the specs, but the current domain requirements model only keeps artifacts and validations. This is not merely backlog; it is a widening seam between the language and the runtime.

### Required work

- Extend the flow/domain model so runtime requirements preserve all promised contract fields.
- Add trusted host-run validation commands and bind their outputs to completion decisions.
- Introduce diff-backed changed-file verification.
- Add duplicate detection and stronger artifact-content validation.
- Persist normalized completion snapshots for both accepted and rejected submissions.
- Make verifier feedback structured enough for deterministic retry loops.

### Acceptance target

No task should enter review because a worker merely claimed that required evidence exists. The host must be able to inspect and prove the basis of acceptance.

## 2. Transactions and Recovery Are Present, but Not Complete

### Spec intent

The atomic-transition plan and MVP contracts call for:

- revision reads;
- locking;
- prepared journals;
- atomic writes where available;
- final reread and validation;
- committed event append;
- stale-lock recovery;
- visible partial failures.

See `spec/11-atomic-transition-plan.md` and `spec/26-mvp-contracts.md`.

### Current implementation

The repository already has:

- `FileTransactionRuntime` with prepared journals and ordered effect application;
- transaction-backed job creation;
- recovery for incomplete job-creation journals;
- adapter-local rollback for some multi-file writes.

See `src/open_tulid/runtime/transactions.py`, `scheduler.py`, and `adapters/obsidian.py`.

### Remaining gaps

The current transaction layer does **not** yet provide:

- project/task revision checks before mutation;
- final trusted reread + validation after mutation;
- generic recovery for completion transactions;
- compensation for partially applied effects;
- a complete policy when artifact promotion succeeds but the later task move or event append fails;
- first-class operator commands to inspect and recover prepared/failed journals.

The most concerning live path is completion acceptance: artifact promotion, task link updates, board moves, and event writes can partially succeed before a later step fails (`src/open_tulid/runtime/completion.py`).

### Required work

- Add project-level mutation locks and revision checks.
- Make each trusted effect idempotent or compensable.
- Add recovery for completion journals, not only job creation.
- Add final reread/validation before a transaction becomes committed truth.
- Surface incomplete transactions in validation and operator commands.
- Define a strict reconciliation policy for every partial-effect boundary.

### Acceptance target

An interruption at any effect boundary should be inspectable and recoverable without hand-editing vault state.

## 3. Runtime Supervision Is the Largest Operational Gap

### Spec intent

`runtime-supervision.md` and the remaining-work plan call for:

- durable container identity;
- live stdout/stderr streaming;
- wrapper heartbeat;
- startup, heartbeat, idle, total, and shutdown timeouts;
- stale-runtime detection;
- inspect/tail/stop/kill controls;
- crash reconciliation after host failure.

### Current implementation

The executor still blocks on a `docker run`-style invocation and writes logs after the worker exits. Runtime commands exist (`runtime start/stop/status`, `jobs daemon`, `jobs logs`), but they do not amount to full supervision.

See `src/open_tulid/runtime/executor.py`, `src/open_tulid/containers/runtime.py`, and `src/open_tulid/cli/main.py`.

### Remaining gaps

- no persisted container ID / image identity / supervisor PID on the job;
- no live log streaming to durable storage;
- no heartbeat distinct from worker output;
- no startup-vs-idle-vs-total timeout model;
- no stale detection for running job without container, container without matching job, dead endpoint, or stale heartbeat;
- no durable detached-runtime recovery model after a host crash;
- no live inspect / stop / kill operator path for a worker already in flight;
- workspace logs can disappear with workspace cleanup.

### Required work

- Replace blocking worker execution with managed process/container supervision.
- Persist runtime identity and health metadata on jobs.
- Stream logs live and promote them before cleanup.
- Implement heartbeat and timeout classes separately.
- Reconcile leftover runtime state at startup and on periodic sweeps.
- Add operator-grade inspect/tail/follow/stop/kill commands.

### Acceptance target

One worker attempt should be observable, controllable, and recoverable while it is still running, without spelunking through disposable workspace internals.

## 4. Scheduler Durability Exists in Embryo, Not Yet in Product Form

### Current strengths

- one active job per task/transition is enforced in storage;
- resource lease acquisition exists;
- leases compose with worker resource declarations;
- a detached daemon exists;
- recovery exists for prepared job creation.

See `src/open_tulid/runtime/scheduler.py`, `resources.py`, and the runtime tests.

### Remaining work

- project/job locking around scheduler decisions beyond the current local file lease path;
- global and per-project concurrency policies as first-class configured limits;
- retry policy with max attempts, retryable classes, backoff, and rejected-completion semantics separate from worker failure;
- scheduling modes such as `--limit`, `--all`, and `--until-empty`;
- durable structured scheduler skip/retry events rather than only console lines;
- stale lease reconciliation beyond “owner file disappeared”;
- clearer degraded-runtime behavior when scheduler or proxy processes die independently.

### Acceptance target

Multiple schedulers must not race the same work, and long-running unattended operation must continue sensibly through transient failures.

## 5. Security and Isolation Need to Catch Up to the Architecture

### What is already good

- secret-like env names are rejected in config;
- long-lived provider credentials remain host-side in the model proxy;
- workers receive scoped model sessions rather than raw provider keys;
- completion tokens are job-bound.

### Current gaps

The current worker invocation is still closer to “containerized execution” than “meaningful sandbox isolation”:

- writable workspace mount;
- no explicit network mode or allowlist;
- no CPU / memory / PID limits;
- no read-only root filesystem;
- no non-root user requirement;
- no `no-new-privileges` or capability minimization;
- completion token appears in persisted command logs;
- proxy body logging can retain sensitive content when configured to `full`.

See `src/open_tulid/containers/runtime.py`, `src/open_tulid/runtime/model_proxy.py`, and `spec/15-sandboxed-worker.md`, `spec/17-http-proxy-endpoint-boundary.md`, `spec/31-secret-handling.md`.

### Required work

- Define the exact supported execution modes and their claims.
- Add redaction before any command/env persistence.
- Add configurable network modes and endpoint allowlisting.
- Add Docker controls for user, rootfs, capabilities, CPU, memory, and PID count.
- Separate completion auth and model-proxy auth cleanly in both implementation and docs.
- Add request auditing with redaction and safe defaults.

### Acceptance target

The docs should be able to say precisely what is isolated, what is merely containerized, and which security claims are valid in each mode.

## 6. Workspace and Git Promotion Model Is Still Unresolved

### Current behavior

`WorkspacePreparer` copies the repository into a disposable workspace. Completion promotion trusts artifact outputs, but accepted source-code changes do not yet have a fully defined trusted route back into the real repository.

See `src/open_tulid/runtime/workspaces.py` and the remaining-work plan Phase 5.

### Why this matters

A project that accepts implementation work but strands code changes in disposable workspaces has not finished the product loop. This is also where verification, recovery, and review all converge.

### Required decision

Choose one product model and finish it:

1. **Disposable workspace + trusted promotion**
   - host computes diff;
   - host validates changed files;
   - host promotes an approved patch/artifact set.

2. **Branch-based workspace + trusted git brokerage**
   - dirty-worktree checks;
   - branch naming and cleanup;
   - diff / patch capture;
   - trusted merge or handoff rules.

### Acceptance target

No successful task should leave meaningful accepted work stranded only in a disposable workspace.

## 7. CLI, Docs, and Operator Experience Need Reconciliation

### Spec drift

The CLI contract promises:

- JSON output for automation;
- proof input for manual transitions;
- state filtering for tasks;
- clean exit-code semantics.

The current CLI surface is richer in some places and thinner in others:

- no general `--json` parity;
- `transition` has no `--proof` path;
- `tasks list` lacks the promised state filter;
- README mostly describes the early product, not the runtime that now exists;
- `transition` mutates storage directly instead of going through the same journaled trusted path used elsewhere.

See `spec/29-cli-contract.md`, `README.md`, and `src/open_tulid/cli/main.py`.

### Operator gaps

- no “why is this task not runnable?” explanation surface;
- no live-follow worker logs;
- no generic recovery command family;
- no unified readiness report that covers proxy, images, workspace permissions, endpoint reachability, and secret hygiene;
- no polished guided flow from install → validate → run → inspect → recover.

### Required work

- bring CLI behavior back into explicit contract alignment;
- add JSON output consistently where automation is expected;
- route manual transitions through the trusted transaction path;
- update README/help/docs to describe the product that now exists;
- distinguish user workflow commands from operator/debug commands.

### Acceptance target

A new operator should be able to configure, inspect, and diagnose the system from docs and CLI output alone.

## 8. Tests Need More Hostile Scenarios, Not Merely More Volume

### What the suite proves well

The current tests show that the core composition works:

- workflow parsing and validation;
- domain and vault validation;
- event and job persistence;
- resource leasing;
- successful completion;
- rejected-then-fixed completion;
- worker exit without completion;
- model proxy session enforcement;
- Docker-backed end-to-end flow.

### What the suite does not yet prove

- crash in the middle of completion effects;
- restart after host death;
- stale heartbeat / dead endpoint / orphan container detection;
- lease expiry and reconciliation under process death;
- duplicate submissions and concurrency races;
- actual trusted verifier behavior against lying workers;
- secret redaction guarantees;
- network/isolation guarantees;
- symlink / path / TOCTOU adversarial cases beyond the current containment checks;
- diff-backed changed-file validation;
- recovery commands and degraded runtime workflows.

### Required work

Build an adversarial test matrix around:

- interruption points;
- concurrency;
- malicious payloads;
- stale runtime artifacts;
- verifier dishonesty cases;
- operator recovery flows;
- overnight-style mocked agent runs.

### Acceptance target

The suite should prove not only that the golden path works, but that the guardrails hold when timing, inputs, and workers are bad.

## 9. Additional Findings Worth Addressing

### 9.1 Manual transition semantics are muddy

`TaskManager.request_transition()` can emit an accepted transition event before the stronger completion requirements are satisfied. That risks conflating “allowed to attempt” with “trusted state move approved.” The specs are more careful than the implementation here.

### 9.2 Event contract drift

The specs describe richer event envelopes and transition/request distinctions than the current implementation consistently emits. In particular, manual transitions and runtime transitions do not always carry the same contextual richness.

### 9.3 Cross-board movement remains unsupported

The Obsidian adapter explicitly rejects cross-board transitions. If that is a deliberate MVP constraint, keep it loudly documented; if not, it is a functional hole.

### 9.4 Domain/CLI diagnostic polish still has loose ends

`docs/spec_review.md` already notes one low-severity issue: duplicate section/field reader errors can be collapsed into generic messages. This is not central, but it is emblematic of the last-mile quality work still ahead.

## Recommended Roadmap

### Phase A — Truth before throughput

1. finish trusted verifier semantics;
2. preserve richer flow requirements in the domain/runtime model;
3. add host-run validation and diff-backed changed-file verification;
4. finish completion-transaction recovery and idempotent effect handling.

### Phase B — Survival under unattended operation

5. implement managed runtime supervision;
6. add heartbeat, stale detection, lifecycle controls, and durable logs;
7. add scheduler retry policy, concurrency, and reconciliation.

### Phase C — Explicit trust boundaries

8. choose and finish the workspace/git promotion model;
9. harden isolation, network policy, resource limits, and redaction;
10. make security-mode claims explicit and testable.

### Phase D — Product finish

11. bring CLI behavior into spec parity;
12. add readiness, recovery, and diagnosis commands;
13. update README/help/docs around the actual runtime.

### Phase E — Adversarial proof

14. expand E2E and fault-injection coverage around all the above;
15. add long-run and crash-recovery acceptance scenarios.

## What “Done Enough to Trust” Looks Like

open-tulid becomes a solid product when all of the following are true:

- a worker cannot move a task forward by merely claiming success;
- a host crash leaves inspectable, recoverable state rather than ambiguity;
- a running job can be observed and controlled while alive;
- two schedulers cannot accidentally schedule the same scarce work;
- accepted code has a defined route back into the canonical repo;
- sandbox and credential claims are precise, enforced, and redacted;
- operators can answer “what happened?”, “what is stuck?”, and “what should I do next?” from the CLI;
- the test suite actively tries to break those promises.

## Source Corpus Reviewed

### Upstream Obsidian specifications and notes

- `/home/rawsteel/repo/obsidian/Agent/spec/00-start-here.md`
- `/home/rawsteel/repo/obsidian/Agent/spec/24-core-invariants.md`
- `/home/rawsteel/repo/obsidian/Agent/spec/26-mvp-contracts.md`
- `/home/rawsteel/repo/obsidian/Agent/spec/29-cli-contract.md`
- `/home/rawsteel/repo/obsidian/Agent/spec/30-agent-instructions.md`
- `/home/rawsteel/repo/obsidian/Agent/spec/31-secret-handling.md`
- `/home/rawsteel/repo/obsidian/Agent/spec/33-workflow-engine-dsl.md`
- `/home/rawsteel/repo/obsidian/Agent/runtime-remaining-work-plan-2026-05-15.md`
- `/home/rawsteel/repo/obsidian/Agent/runtime-supervision.md`
- `/home/rawsteel/repo/obsidian/Agent/model-http-proxy.md`

### Repository docs and implementation

- `README.md`
- `docs/app-installation-spec.md`
- `docs/domain-cli-integration-spec.md`
- `docs/spec_review.md`
- `src/open_tulid/cli/main.py`
- `src/open_tulid/runtime/{scheduler,executor,completion,transactions,resources,model_proxy,verifier,workspaces}.py`
- `src/open_tulid/containers/runtime.py`
- `tests/runtime/*`
- `tests/e2e/test_docker_mock_agent_runtime.py`

## Closing Assessment

The project is strong enough now that the most valuable work is not glamorous. It is the work that turns a clever runtime into a calm one: truth-preserving verification, boring recovery, explicit boundaries, and operators who never need to guess what happened.
