# Plan 1 — Reliable worker execution and recovery

Status: implementation plan; no runtime changes have been made by writing this document.

Parent: [reliability and polish plan](reliability-polish-plan.md). This covers execution reliability, credential lifetime, failure reporting, and bounded recovery.

## Outcome and boundaries

Configured `<workers>` can use their assigned model for the entire permitted attempt. Every attempt ends in accepted completion, a bounded repair/retry, or an actionable terminal result. A daemon restart does not erase the retry budget or the evidence needed to recover work.

Planning, implementation, and review are workflow responsibilities assigned to `<workers>`. Worker IDs, model names, executables, and providers remain configuration. Provider/tool-specific classification belongs in its adapter. The scheduler consumes generic outcomes and never branches on the configured model name.

Keep existing permission protections, resource capacity, workflows, and project commands. Do not introduce a second scheduler, unlimited sessions, automatic model substitution, or a new monitoring UI.

## Starting evidence and code

- `runtime/model_proxy.py`: session stores default to 3,600 seconds; `get` discards expired sessions, and `forward` returns unauthorized before writing a normal transcript.
- `runtime/executor.py`: sessions are issued per worker run; generic exit handling loses the detailed cause. Completion, liveness observation, cleanup, and repair may race.
- `runtime/scheduler.py`: failure counting can start at the runtime session boundary. Completion repair and fresh-worker attempts have separate controls.
- `runtime/repairs.py`, `runtime/jobs.py`, `runtime/resources.py`, `runtime/observability.py`: existing persistence, repair, leasing, and liveness mechanisms to extend.
- `containers/runtime.py`, `models.py`, `config.py`, `cli/main.py`: worker timeouts, configuration resolution, runtime construction, and operator output.

Wealthy Scholar allows 7,200-second workers. Two saved attempts of task #8 failed just after an hour with an unauthorized message. Expiry is a strong hypothesis; historical authentication logs are insufficient to prove the cause.

## Decisions

1. Use a bounded credential for each worker attempt. Set its explicit expiry from the attempt deadline plus the configured completion-settlement allowance. Issue a fresh credential for a new process attempt, including a repair process. Keep active lease checks and revoke the old credential when that process is settled. This avoids introducing renewal before it is necessary.
2. Persist wall-clock deadlines for restart recovery; use monotonic elapsed time within a running process. Reject nonpositive/unbounded attempt durations for this managed execution path. Document behavior for existing configurations before changing defaults.
3. Separate an attempt from a completion submission. An invalid/replayed HTTP submission must not consume a worker retry by itself. Starting another worker process consumes one total attempt, whether it resumes a patch or starts clean.
4. Resolve one total attempt limit for a task revision and transition. Count the initial attempt as one. Preserve existing repair limits as sublimits until migration is complete; neither path may exceed the total. Print the effective policy so old settings cannot be interpreted silently.
5. An accepted completion wins over a late exit/failure notification. Once terminal state is persisted, duplicate cleanup and observation must be idempotent.

## Implementation sequence

### 1A. Pin a baseline and define the attempt record

Record the source revision and dirty-patch identity, installed Tulid location, workflow hash, command-policy hash, worker image identity, and sanitized effective worker configuration in an isolated fixture run. Do not assume the installed runtime is the checkout being edited.

Extend existing job metadata/storage with a versioned attempt record: task revision, transition, attempt number, predecessor, start/deadline/end, worker identity, status, and failure reference. Task revision must exclude board state, timestamps, and generated audit links; use the semantic revision defined in [plan 3](reliability-03-task-quality-plan.md).

Persist attempt admission under the existing store/lease coordination before spawning the worker. Recovery must distinguish an admitted attempt whose launch was interrupted from a worker that actually ran, without allowing duplicate starts. A manual daemon restart cannot create a new budget identity.

### 1B. Correct session creation and rejection evidence

Pass explicit attempt expiry through both session stores. Inject a clock into expiry decisions for deterministic tests. Make file-backed and in-memory behavior equivalent. Exercise cleanup during validation and recursive/current repair paths so revoking an old attempt cannot revoke a newly issued credential accidentally.

Return a typed session lookup outcome internally: valid, expired, unknown, wrong proxy, or lost lease. Log timestamp, safe job/attempt correlation when known, reason, and HTTP outcome before returning. Do not log bearer tokens or use a token itself as a correlation ID. Unknown/revoked may remain a combined outcome when retained metadata cannot distinguish them; never invent a reason.

Keep invalid or expired sessions unable to forward requests. A request already streaming at the deadline has explicitly bounded transport behavior; the next request must revalidate the session. Worker timeout and process cleanup remain authoritative.

### 1C. Classify failures at the executor boundary

Introduce a small generic result carrying `code`, `category`, `retryable`, `retry_action`, sanitized evidence, and evidence path. Suggested categories are authentication, provider, permission, context, timeout, environment, implementation, and protocol.

Read structured proxy/adapter evidence first, then explicit worker-tool events, then sanitized log signatures. Classify a doom-loop denial only from a corresponding event/signature; an unauthorized string plus restrictive configuration is insufficient. A test that intentionally prints an authentication-like string must not become a provider failure.

Attach the result to the job and execution event before scheduler handling. Keep the numeric return code for compatibility. Classify exit without completion separately from nonzero exit; settle an in-flight completion before deciding either.

### 1D. Apply bounded recovery by cause

| Cause | Recovery |
| --- | --- |
| Transient upstream/service failure | Fresh process within the total budget, preserving the previous evidence and any candidate patch. Honor explicit provider retry timing where supplied. |
| Expired managed credential during an otherwise valid attempt | Report the infrastructure defect; retry with a correctly bounded credential. Repetition exhausts the budget rather than looping. |
| Failed project behavior/check | Resume the candidate with precise check feedback, subject to repair and total limits. |
| Missing tool/dependency or invalid configuration | Stop with the environment/configuration blocker; do not ask the worker to rewrite application behavior. |
| Context exhaustion | Retry only after producing a valid reduced packet under plan 2. Never drop required context to force a retry. |
| Permission denial or unknown unauthorized | Stop unless explicit evidence supports a bounded corrective retry; do not relax permissions automatically. |
| Missing product/architecture decision | Preserve the attempt and return an actionable planning blocker; no speculative implementation retry. |
| Operator stop | Persist interruption and preserve work. A later explicit resume is permitted, but does not erase already consumed attempts. |

Replace recursive restart behavior with an explicit bounded attempt loop if needed to keep cleanup, credentials, and counters unambiguous. Reuse the existing job/completion machinery rather than building a competing engine.

### 1E. Preserve evidence before cleanup and reconcile restarts

Before deleting or scrubbing a failed workspace, persist logs, the task/context identity, failure record, and recoverable source changes. Use the manifest/change-set implementation from [plan 5](reliability-05-delivery-plan.md) when available; until then preserve the workspace itself rather than pretending an incomplete patch is sufficient.

On restart, reconcile process existence, active completion validation, lease ownership, and persisted attempt state. Reattach only when existing runtime support can do so safely; otherwise settle interruption and start a bounded new attempt. Report the action through existing jobs/runtime commands.

## Regression and acceptance checks

- Fake clock: a permitted two-hour attempt still authenticates at 59, 61, and 119 minutes; it fails after its explicit expiry and immediately after revocation or lease loss.
- File store restart: expiry, attempt count, and exhausted status survive process reconstruction.
- Two schedulers race to admit the same task revision: only one worker starts and only one attempt is charged.
- Accepted completion races nonzero exit, liveness failure, timeout, and duplicate events: acceptance remains stable and cleanup occurs once.
- Repair uses a fresh credential; cleanup of its predecessor cannot invalidate it.
- Invalid HTTP completion replay does not launch or charge another worker process.
- Seeded logs distinguish upstream authentication, explicit tool denial, ordinary failing tests, timeout, and ambiguous unauthorized without leaking credentials.
- Mixed repair/fresh retries stop at the total bound; restarting the daemon does not renew it.
- Failed and stopped workers retain readable work and evidence before any destructive cleanup.

Extend `tests/runtime/test_model_proxy.py`, `test_executor.py`, `test_jobs_scheduler.py`, `test_repairs.py`, `test_resources.py`, and `test_observability.py`. Run the deterministic suite, then the controlled sustained worker exercise in [plan 6](reliability-06-product-completion-plan.md).

## Migration and completion evidence

Read legacy jobs without inventing precise attempt history. For an existing unfinished revision, account for identifiable attempts and mark ambiguous history explicitly; do not silently reset it. Preserve old configuration keys with documented translation and diagnostics for conflicting limits. Reconfigure only future attempts; retain historical packets and logs.

Deliver a PR with the resolved lifetime formula and retry semantics, fake-clock/race-test results, a sanitized classified incident, and a restart trace. This plan is complete only after a controlled `<workers>` run exceeds one hour without losing active-job credentials and every tested failure produces a bounded, truthful result.
