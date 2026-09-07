# Worker Observability (Liveness-Only) Implementation Plan

**Repo:** `open-tulid` at `/home/rawsteel/repo/open-tulid`
**Branch:** `master` (working tree dirty — an in-progress observability implementation is present)
**Status:** Review and planning only. No source/config/test/template changes, no runtime processes started or stopped.

This document reviews the current, uncommitted worker-observability implementation against 14 liveness-only requirements, identifies gaps, and gives an ordered execution plan for a later agent. It does **not** assume the existing code is correct; each requirement is scored against the actual source.

---

## 1. Executive summary

The core liveness-only observability module already exists and is mostly well-designed. `src/open_tulid/runtime/observability.py` is a distinct executor-owned supervisor that probes real worker/container liveness, is keyed by `(job_id, attempt_id)`, treats all unexpected exits uniformly, never parses OpenCode logs or `doom_loop`/`Unauthorized` text, and does no cleanup or killing. The executor (`src/open_tulid/runtime/executor.py`) registers/owns workers, performs the failed-worker flow (fail → stop container → scrub workspace → release lease), and leaves the Kanban task untouched. The scheduler (`scheduler.py`) no longer inspects worker logs or per-task contracts.

Scores (1 = fully met, 0 = not met, partial = partially met):

| Req | Verdict | One-line evidence |
|----|--------|-------------------|
| 1 distinct module under runtime | ✅ met | `runtime/observability.py` |
| 2 executor owns worker/obs/failure/cleanup/lease | ✅ met | `executor.py:658-819` |
| 3 scheduler only selects/starts jobs | ✅ met | `scheduler.py:86-280`; no log/doom-loop read |
| 4 bounded configurable interval (~1min prod, short tests) | ✅ met | `observability.py:138` (60.0); tests use 0.001–0.01 |
| 5 no log parsing / no doom-loop classification | ✅ met (dead code remains) | health path reads status only; `opencode_classifier.py` orphaned |
| 6 accepted completion / validation safe | ✅ met | `observability.py:106-115`; `executor.py:448` |
| 7 atomic fail+exit-info+scrub+lease-release+TODO-preserved | ✅ met | `executor.py:771-819` |
| 8 no arbitrary cooldown introduced | ⚠️ met for observability, conflict with pre-existing scheduler backoff | `scheduler.py:29,198-221`; `models.py:24` |
| 9 no per-task contract reintroduced | ✅ met | removed from scheduler (diff) |
| 10 keyed by job_id+attempt | ✅ met | `observability.py:139,164-180` |
| 11 immediate exits / duplicate harmless / stale isolated | ✅ met | `observability.py:99-122,164-167` |
| 12 logs diagnostic-only | ✅ met | `executor.py:948-950` write-only |
| 13 executor sole killer/cleaner | ✅ met | observability emits `WorkerExited` only |
| 14 tests prove real executor/obs/scheduler path | ⚠️ partial | observability tests isolated; scheduler tests isolated; **no proactive-liveness integration test** |

**Main gaps, in priority:**
- **G14:** No test drives the real `executor → observability → scheduler` flow for an *unexpected* exit (liveness probe flips dead mid-run). Existing executor tests exercise completion/validation return paths, not the `WorkerExited` proactive path.
- **G8:** Even though observability adds no cooldown, the scheduler's pre-existing `failed_job_backoff_seconds` (default 60) applies to every FAILED job, so after an unexpected worker exit the fresh attempt is skipped for ~60s by default. This collides with the "no cooldown → fresh attempt" spirit of the requirement and must be made an explicit decision.
- **G5/G9 migration:** `src/open_tulid/runtime/opencode_classifier.py` + `tests/runtime/test_opencode_classifier.py` are orphaned/remnant, and `tests/runtime/test_jobs_scheduler.py:845` still stimulates `opencode.doom_loop` metadata that nothing writes. Dead code + stale tests must be removed or quarantined.

---

## 2. Current modules, classes, functions (with file:line)

### `src/open_tulid/runtime/observability.py` (new, untracked, 196 lines)
- `TERMINAL_EXECUTOR_STATUSES = {accepted, failed, stale, cancelled}` — line 9
- `COMPLETION_UNDER_VALIDATION_STATUSES = {completion_submitted}` — line 10
- `WorkerExited(job_id, attempt_id, returncode=None)` — lines 13–23 (the emitted fault event)
- `WorkerLivenessProbe.is_alive() -> bool` (Protocol) — lines 26–33
- `_ObservedWorker` — lines 36–126; `start()` 77, `stop()` 85, `_run()` 88, `_check_once()` 99, `is_thread_alive()` 124
- `WorkerObservability(check_interval_seconds=60.0)` — lines 129–187; `register()` 142, `unregister(job_id)` 170, `has_worker()` 178, `close()` 182
- Exported from package: `runtime/__init__.py:105-111`, `235-237`

### `src/open_tulid/runtime/executor.py` (modified, dirty)
- `TERMINAL_JOB_STATUSES` — lines 53–58; `COMPLETION_SETTLE_STATUSES` — 60–62; `_WORKER_POLL_SECONDS = 0.05` — 66
- `JobExecutor.__init__` sets up executor-owned observability and optional injected probe — lines 200–247 (`observability` 213, interval fallback 236–246)
- `run()` orchestration — lines 249–529
  - status → RUNNING + `increment_attempts=True` — line 360–380 (this is the per-attempt identity source: `attempt_id = str(job.attempts)`)
  - calls `_run_worker_monitored` — line 442–446
  - `_wait_for_completion_settlement` after success — line 448
  - worker-vanished (result is None) reconciliation — line 452–457
  - terminal-outcome reconcile + idempotent scrub — line 491–499
  - no-completion exit → `_fail_completed_worker_without_completion` — line 503
  - finally: `release_job` + `revoke_job` — line 525–529
- `_wait_for_completion_settlement` — lines 531–548
- `_start_completion_endpoint` — 550–580
- `_run_worker_monitored` — lines 658–737: registers probe keyed by `(job_id, attempt_id)` before `thread.start()` (712–719); waits on `exit_event` vs thread-alive (722–730); on `WorkerExited` calls `_fail_worker_after_unexpected_exit` (725); finally `unregister` (732)
- `_fail_worker_after_unexpected_exit` — 739–748
- `_fail_completed_worker_without_completion` — 750–769
- `_fail_worker` (atomic, idempotent fail+event+stop+scrub+lease) — 771–819
- `_stop_worker` (sole killer, `stop_worker_container`) — 821–830
- `_RunLifecycleLivenessProbe` (thread-alive probe) — 836–856
- log writing (write-only, no parsing for failure) — 948–950, 1014–1016
- `_scrub_workspace_for_job` (idempotent `shutil.rmtree`) — 1577–1589
- OpenCode agent config writes `doom_loop: deny` (config only, not classification) — 1544–1549

### `src/open_tulid/runtime/scheduler.py` (modified, dirty)
- `RECENT_FAILURE_BACKOFF_SECONDS = 60` — line 29
- `schedule_one` / `_schedule_one_locked` — 86–280
  - serial repo lane focus — 100–134
  - dependency + transition selection — 136–146
  - active-job check (FAILED jobs are not active) — 148–170
  - retry-limit check — 172–196
  - **recent-failure backoff skip** — 198–221 (`_find_recent_failed_job`)
  - create job + lease admit — 223–278
- `_find_recent_failed_job` — 448–479 (uses `backoff_seconds`)
- `_find_failed_jobs` — 481–504
- Worker failure is **uniform** (no classification consumed anywhere)

### `src/open_tulid/models.py`
- `RuntimeConfig.worker_liveness_check_interval_seconds: float = 60.0` — line 37
- `RuntimeConfig.failed_job_backoff_seconds: int = 60` — line 24

### `src/open_tulid/cli/main.py`
- `run_job` builds `JobExecutor` — 642–674 (does **not** pass `worker_liveness_check_interval_seconds`; it is read from `config.runtime` inside the executor)
- scheduler/daemon loops call `Scheduler.schedule_one` + `executor.run` separately — 426–470, 677–775

### `tests/`
- `tests/runtime/test_observability.py` (268 lines) — isolated observability helper tests
- `tests/runtime/test_executor.py` — real executor path for completion/validation (275, 557), but not the proactive-liveness failure path
- `tests/runtime/test_jobs_scheduler.py` — scheduler failed-worker behavior (760–909)
- `tests/runtime/test_opencode_classifier.py` — orphaned classifier tests (remnant)
- `tests/e2e/test_runtime_detached_stt_workflow.py` — e2e workflow; no liveness-failure scenario

---

## 3. Gap table

| # | Requirement | Current evidence | Gap | Proposed change |
|---|---|---|---|---|
| 1 | distinct module under runtime | `runtime/observability.py` exists | none | keep |
| 2 | executor owns process/registration/failure/cleanup/lease | `executor.py:658-819` | none | keep; add explicit ownership comment at registration (712) |
| 3 | scheduler only selects/starts; no log/doom inspection | `scheduler.py` has no log reads; `opencode_classifier` not imported | none | keep |
| 4 | bounded configurable interval | `models.py:37`, `observability.py:138`, executor fallback `236-246` | CLI never passes the kwarg (relies on config object) — acceptable but implicit | optionally pass `worker_liveness_check_interval_seconds` in `cli/main.py` run_job for explicitness |
| 5 | no log parsing / no doom-loop classification | health path reads `job_store.get` status only (`observability.py:106`) | dead module `opencode_classifier.py` + scheduled `doom_loop` test fixtures remain | delete `opencode_classifier.py` + `test_opencode_classifier.py`; purge `doom_loop`/`failure_code` fixtures in `test_jobs_scheduler.py:845-865` |
| 6 | accepted/validation safe | `TERMINAL_EXECUTOR_STATUSES`, `COMPLETION_UNDER_VALIDATION_STATUSES` (9–10); `executor.py:448` | small TOCTOU: a `WorkerExited` racing into `_fail_worker` while status is `completion_submitted` would fail an imminent acceptance, because `_fail_worker` (794) only guards ACCEPTED/TERMINAL, not COMPLETION_SUBMITTED | add `COMPLETION_SUBMITTED` to the safe/guard set in `_fail_worker` (or re-check status inside `_fail_worker` and no-op) |
| 7 | atomic fail + exit info + scrub + lease + TODO-preserved | `_fail_worker` 771–819: `update_status(FAILED)`, `event` with returncode, `_stop_worker`, `_scrub_workspace_for_job`, `release_job`; no `move_task` anywhere | for non-lease runs, workspace scrub and container stop are best-effort (fine); correctness holds | add a test asserting task state unchanged + returncode in metadata/event |
| 8 | no arbitrary cooldown introduced | observability adds none | pre-existing `failed_job_backoff_seconds=60` (`scheduler.py:29,198-221`, `models.py:24`) delays the fresh attempt after every worker FAILED by default | **explicit decision needed:** (a) keep the bounded retry throttle as *intended* (not "arbitrary") and document it, or (b) exempt `worker_unexpected_exit` failures from the backoff via a metadata flag. Do NOT silently change default. |
| 9 | no per-task contract reintroduced | scheduler per-task contract path removed (see git diff; `test_jobs_scheduler.py:401-490`) | none | keep; run contract tests to confirm |
| 10 | keyed by job_id+attempt | `(job_id, attempt_id)` dict — `observability.py:139,164-180`; `attempt_id=str(job.attempts)` — `executor.py:690` | none | keep |
| 11 | immediate exits / duplicate harmless / stale isolated | register-before-start (712–719); `_handled` lock (73–122); per-attempt keys | probe is thread-alive based; a hung-but-alive container or a re-used quick thread could report ambiguous liveness | document thread-alive limitation; keep `_handled` idempotency |
| 12 | logs diagnostic-only | `executor.py:948-950,1014-1016` write-only; no health parse | none | keep |
| 13 | executor sole killer/cleaner | observability only calls `probe/read_status/on_exited` — never containers/workspaces | none | keep |
| 14 | tests prove real executor/obs/scheduler path | observability tests isolated; executor tests exercise completion/validation return path (`,test_executor...:275,557`) | **no test drives the proactive liveness-failure → `_fail_worker_after_unexpected_exit` → scheduler fresh attempt** | add executor-level + integration tests (see §10) |

---

## 4. Proposed module/API boundaries and ownership

```
open_tulid.runtime.observability     → liveness supervisor (publish-only)
  WorkerExited(job_id, attempt_id, returncode)
  WorkerLivenessProbe                → injectable Protocol (thread/process/container)
  WorkerObservability                → register/unregister, periodic check

open_tulid.runtime.executor          → sole owner of worker lifecycle
  owns: worker start/stop (kill), workspace scrub, lease release, status/events
  registers probe with observability keyed (job_id, attempt_id)
  consumes WorkerExited → _fail_worker_after_unexpected_exit → _fail_worker

open_tulid.runtime.scheduler         → selection/creation only
  selects runnable tasks, starts jobs, applies bounded retry policy
  no log reads, no doom-loop decision, no contract regeneration

open_tulid.models.RuntimeConfig      → worker_liveness_check_interval_seconds (60.0)
                                     → failed_job_backoff_seconds (60, decision pending)
```

Ownership rules:
- **Observability never** starts/stops/kills/cleans/leases; it only emits `WorkerExited`.
- **Executor is the only** component permitted to call `stop_worker_container` and `_scrub_workspace_for_job`.
- **Scheduler** acts only on job status from the job store (FAILED, ACCEPTED, no active) and on `runtime_session_started_at`; it must never read worker log content.

---

## 5. Worker lifecycle / state machine (with races and idempotency)

Per-attempt state machine keyed by `(job_id, attempt_id)`:

```
job created (PENDING)
  → executor.run()
  → status RUNNING, increment_attempts (attempt_id = job.attempts)   executor.py:360-380
  → register(probe) with WorkerObservability                         executor.py:712
  → worker/container starts, thread runs
  ─ ─ periodic probe at interval ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  alives?
    ├─ yes → continue idle
    ├─ no, status ∈ {accepted, failed, stale, cancelled} → normal/terminal, handled, no fault
    ├─ no, status = completion_submitted → keep alive (validation in flight)
    └─ no, else → emit WorkerExited(job_id, attempt_id, returncode)
  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
on WorkerExited while run thread in flight:
  _fail_worker_after_unexpected_exit → _fail_worker(FAILED, stop+scrub+lease)   executor.py:725,739
run thread returns before exit_event:
  success → _wait_for_completion_settlement                                  executor.py:447-448
  terminal (ACCEPTED) → return result, unregister                            executor.py:476-477
  COMPLETION_REJECTED + repair_ready → restart same frozen job (preserved ws) executor.py:478-490
  FAILED/STALE/CANCELLED → reconcile + idempotent scrub                      executor.py:491-499
  no completion + nonzero → _fail_completed_worker_without_completion        executor.py:503
finally: unregister + lease release + session revoke                          executor.py:525-529,732
```

**Races and idempotency:**
- **Duplicate health notification:** `_handled` guarded by a lock (`observability.py:73-122`) → single emit; executor `_fail_worker` re-checks status (`executor.py:791-795`) and is idempotent on repeat calls (accepted/terminal guard).
- **Exit vs. completion race:** `on_exited` bails if `result`/`error` already in `result_box` (`executor.py:701`); the run’s own post-processing wins. If `_fail_worker` already ran, the post-run terminal branch (491) re-scrubs idempotently (`1577-1589`).
- **Stale attempt cannot affect current:** keyed dict + `register` stops any prior worker for the same key (`observability.py:164-167`); the job id differs per fresh attempt created by the scheduler.
- **Immediate exit:** `register` happens before `thread.start()` (`executor.py:712-720`); `_RunLifecycleLivenessProbe.is_alive` reports `True` while `ident is None` (`executor.py:852-855`) so the just-started worker is not spuriously failed pre-start.
- **Missed callback / scheduler stuck-active:** not possible here because failure is written synchronously to the job store (FAILED) by the executor, and the scheduler keys on job store status (`scheduler.py:148`).
- **Open TOCTOU (gap 6):** `_fail_worker` guard does not include `completion_submitted`; close by re-checking status inside `_fail_worker`.

---

## 6. Liveness signal and polling mechanism

- **Signal:** `WorkerLivenessProbe.is_alive() -> bool` — "is the worker process/container actually running?" Never GPU usage, never stored job status, never parsed log text.
- **Default probe** `_RunLifecycleLivenessProbe` (`executor.py:836-856`): reports alive while the foreground worker run thread is in flight (`thread.is_alive()`); `True` before the thread begins.
- **Polling:** `_ObservedWorker._run` loops: check → sleep `check_interval_seconds` → until stopped or handled (`observability.py:88-98`). Interval bounded at `max(0.001, …)` and defaults to 60s (`observability.py:138`); configured in tests at 0.001–0.01 and overridable via `RuntimeConfig.worker_liveness_check_interval_seconds` (`models.py:37`).
- **Note:** the default probe is thread-alive based, not process-alive based; for a Docker worker it reflects the foreground `docker run`. A process that is alive-but-hung, or a detached/orphaned container, would report alive. Acceptable for the current model; documented as a limitation (see §12).

---

## 7. Definition: normal exit vs unexpected exit

- **Normal exit** = worker/container exits while (or after) the job has reached an **accepted completion** or a terminal outcome already recorded (accepted/failed/stale/cancelled), or a completion is **under validation** (`completion_submitted`). `observability._check_once` returns without emitting for these (`observability.py:106-115`).
- **Unexpected exit** = probe reports not-alive while job status is *not* accepted, *not* terminal, and *not* `completion_submitted` — i.e., the worker vanished with **no accepted completion** in hand.
- All unexpected exits are **equivalent**, regardless of cause (doom_loop, Unauthorized, crash, permission, kill). No log text is read to decide.
- **Completion-under-validation safety (req 6):** when the worker exits during `completion_submitted`, the supervisor keeps the job alive until validation resolves to accepted/terminal (`observability.py:114-115`), and the executor additionally calls `_wait_for_completion_settlement` after a successful run (`executor.py:448`).

---

## 8. Executor callback/event and cleanup sequence (atomic)

`WorkerExited(job_id, attempt_id, returncode)` → executor handler `_fail_worker_after_unexpected_exit` → `_fail_worker`:

1. **Re-load job**; return silently if missing or already accepted/terminal (`executor.py:788-795`) — idempotency gate.
2. **Mark FAILED** atomically: `job_store.update_status(FAILED, metadata={worker_returncode?, failure_reason:"worker_unexpected_exit"})` (`executor.py:803`).
3. **Emit `ExecutionFailed` event** carrying `returncode`, `reason` (`executor.py:804-813`). Structured basic exit info preserved.
4. **Kill worker** (sole killer): `_stop_worker` → `containers.stop_worker_container(docker_executable, job_id)` best-effort (`executor.py:814-830`).
5. **Scrub workspace** idempotently: `_scrub_workspace_for_job` → `shutil.rmtree` no-op if missing (`executor.py:816-817,1577-1589`). Durable task history/events untouched.
6. **Release lease**: `lease_store.release_job(job_id)` (`executor.py:818-819`); also released in `finally` (`525-529`).
7. **Kanban task left as-is** — the executor never calls `adapter.move_task` anywhere in this flow; a Todo task stays Todo.

`WorkerObservability.unregister(job_id)` is called from `_run_worker_monitored`’s `finally` (`executor.py:732`) to stop the supervisor thread when the job finishes either way.

---

## 9. Scheduler behavior after lease release (no cooldown)

- After `release_job` + FAILED, the task has **no active job** (`scheduler.py:148-170`), so the next `schedule_one` treats the same task as runnable again and creates a **fresh job id / attempt** (`scheduler.py:223-278`).
- The scheduler **does not** inspect worker logs or attempt classification; worker failures are uniform.
- **Conflict (gap 8):** `_find_recent_failed_job` (scheduler.py:448-479) will suppress the fresh attempt for `failed_job_backoff_seconds` (default **60**, `models.py:24`) because the unexpected exit marks the job FAILED. This is a pre-existing bounded throttle, not introduced by observability; but by default it re-introduces ~60s latency before the "fresh attempt" the requirement wants.
- **Decision (must be explicit):** either
  - (A) accept the 60s as an *intended bounded retry throttle* and document it (the requirement only forbids *arbitrary* new cooldowns), or
  - (B) mark `worker_unexpected_exit` FAILED jobs with `metadata["no_backoff"] = True` / distinct failure_reason and have `_find_recent_failed_job` skip them so the fresh attempt is immediate.
  - Tests must assert whichever is chosen: `test_scheduler_failed_worker_yields_fresh_attempt_without_backoff` (backoff=0) already proves option (B) with `failed_job_backoff_seconds=0` (`test_jobs_scheduler.py:786`).

---

## 10. Test plan

**A. Extend the real executor path (requirement 14).** In `tests/runtime/test_executor.py` add a test that runs the real `JobExecutor` with a `WorkerObservability` injected at a short interval and a controllable `WorkerLivenessProbe`; the probe starts alive inside `fake_run_agent_container` and flips dead mid-run while no completion is submitted.

- `test_executor_unexpected_worker_exit_fails_job_and_scrubs_and_releases_lease(tmp_path, monkeypatch)`
  - Acceptance: `result.accepted is True` with `run is None`; job status `failed`; metadata `failure_reason == "worker_unexpected_exit"` and `worker_returncode` present; workspace directory removed; `release_job` called (assert lease store empty); **no** `ExecutionAccepted` event.
- `test_executor_duplicate_worker_exit_notices_are_harmless(...)`
  - Acceptance: emitting `WorkerExited` twice calls the fail flow once; job not failed a second time; single `ExecutionFailed` event.
- `test_executor_worker_exit_after_accepted_completion_is_normal(...)`
  - Acceptance: submit accepted completion then flip probe dead → job `accepted`, no FAILED event (real executor path + obs).
- `test_executor_worker_exit_under_completion_validation_is_kept_alive(...)`
  - Acceptance: job `completion_submitted` → probe dead → no failure; validation later → accepted.

**B. Scheduler fresh-attempt (no cooldown, per chosen decision).** In `tests/runtime/test_jobs_scheduler.py`:
- If decision (B): `test_scheduler_fresh_attempt_after_worker_unexpected_exit_is_immediate` — a FAILED job with `failure_reason="worker_unexpected_exit"` under default `failed_job_backoff_seconds=60` still schedules a new job immediately.
- Keep current tests: `test_scheduler_failed_worker_yields_fresh_attempt_without_backoff` (786), `test_scheduler_failed_worker_respects_configured_backoff` (817).

**C. End-to-end (real executor+observability+scheduler).** Add to `tests/runtime/test_jobs_scheduler.py` or a new integration test driving a thread/process worker fixture whose process exits early:
- `test_e2e_worker_dies_without_completion_driver_schedules_fresh_attempt` — scripted worker exits code 1 with no POST; driver runs `schedule_one → executor.run → schedule_one`; assert first FAILED + second job created (new job_id), task unchanged.

**D. Migration/remnant cleanup tests.** Delete `tests/runtime/test_opencode_classifier.py`; strip `opencode.doom_loop`/`failure_code` fixtures from `test_jobs_scheduler.py:845-865`.

**E. Keep existing passing suites green:** `test_observability.py` (28 pass), `test_jobs_scheduler.py` (35 pass), focused executor completion/validation tests (verified passing).

---

## 11. Migration / compatibility concerns (incl. `opencode_classifier` remnants)

- **`opencode_classifier.py`** is fully orphaned: no import in `src/` (only `tests/runtime/test_opencode_classifier.py`), not exported from `runtime/__init__.py`, and no executor/scheduler consumer. The health/liveness path never parses logs. It is safe to delete; leaving it risks future regression toward textual classification (req 5). Delete module + test.
- **Config `doom_loop: "deny"`** (`executor.py:1548`) is the OpenCode worker *agent permission config* and is unrelated to classification — it must remain (it is not a log-parse or decision source).
- **Log files** (`agent.log`, `stdout.log`, `stderr.log`) remain written (`executor.py:948-950`) as diagnostics only; their schema/docs should say "diagnostic, not health signal".
- **Scheduler test fixtures** at `test_jobs_scheduler.py:845` reference `failure_code="opencode.doom_loop"`/`retryable` in metadata that nothing writes; either remove or rename to neutral `failure_reason`. Keeping them implies the scheduler consumes structured classification it does not — misleading.
- **`failed_job_backoff_seconds` default 60** is a compatibility behavior; changing to 0 (option B) alters existing retry-throttle semantics for *all* failures, not just worker exits. If changed, only exempt `worker_unexpected_exit`/explicit flag to avoid changing unrelated backoff behavior; otherwise keep default and document as intended.
- **CLI wiring:** `cli/main.py` does not pass `worker_liveness_check_interval_seconds` to `JobExecutor`; the value is honored only because the executor reads it from `runtime` (`executor.py:236-245`). Compatible, but pass it explicitly or document the runtime-config coupling to avoid drift.
- `attempt_id = str(job.attempts)` (`executor.py:690`) depends on the RUNNING status transition incrementing attempts (`360-380`); any refactor of attempt tracking must preserve this to keep req 10 true.

---

## 12. Risks and explicitly deferred decisions

**Risks**
- **R1:** The default probe is thread-alive based (`executor.py:836-856`). Hung-but-alive workers or orphaned containers report alive → missed failures. Deferred: introduce a process/PID or Docker `inspect` liveness probe; do not block other work.
- **R2:** The `completion_submitted` TOCTOU in `_fail_worker` (gap 6) could fail a job the instant before acceptance under a tight race. Low probability; close by re-checking status in `_fail_worker`.
- **R3:** If decision (B) (no backoff for worker exits) is taken but implemented by checking a flag, a FAILED job written by an older process without the flag would still back off — forward-compat: normalize by `failure_reason`.
- **R4:** Tests for the proactive path rely on an injected probe; the real `_RunLifecycleLivenessProbe` (thread alive) is not directly validated — mitigate by an integration-style test using an actual subprocess worker.
- **R5:** No CLI/e2e path currently constructs the executor with an explicit `observability` instance; production uses the executor's internal singleton. Keep injection points stable for tests.

**Explicitly deferred**
- A PID/Docker-inspect based probe replacing thread-alive.
- Whether `failed_job_backoff_seconds` should default to 0 or exempt worker-exit failures globally (decision D in §9).
- Orphaned/`stale` job reconciliation beyond the current terminal branch (stale jobs are currently caught by the scheduler as blockers at `test_file_execution_job_store:310`).
- Moving the observability module to a separate package (it is correctly under `runtime/`; no move desired).

---

## 13. Minimal ordered implementation steps (for a later agent)

1. **Remove classifier remnants:** delete `src/open_tulid/runtime/opencode_classifier.py` and `tests/runtime/test_opencode_classifier.py`.
2. **Purge stale scheduler fixtures:** in `tests/runtime/test_jobs_scheduler.py`, strip `opencode.doom_loop`/`failure_code` metadata from the retry-limit test (845–865) so nothing references classification.
3. **Close the `completion_submitted` TOCTOU:** in `executor._fail_worker` add `COMPLETION_SUBMITTED` (or a re-read) to the no-fault guard set; add a comment.
4. **Add real-executor proactive-failure tests** in `tests/runtime/test_executor.py`: unexpected-exit fail+scrub+lease, duplicate-notice harmless, accepted-exit normal, validation-kept-alive (cases in §10.A).
5. **Decide the backoff behavior (D in §9)** and implement + test: either (A) document 60s as intended, or (B) exempt `worker_unexpected_exit` from `_find_recent_failed_job` and add the immediate-fresh-attempt test.
6. **Add an integration/e2e test** driving schedule → run → unexpected exit → schedule for a fresh attempt of the same task with Kanban state unchanged (§10.C).
7. **Wire `worker_liveness_check_interval_seconds` explicitly** in `cli/main.py` `run_job` for explicitness (optional, low risk).
8. **Run the full deterministic suite** (`pytest tests/runtime tests/e2e -q`, e2e requires Docker/skipped as appropriate) and report exact files and results.

---

*All file:line references verified against the current working tree; unit test results: `test_observability.py` 28 passed, `test_jobs_scheduler.py` 35 passed, 2 focused executor completion/validation tests passed.*
