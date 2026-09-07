# Plan 5 — Deliver exactly the verified changes

Status: implementation plan; repository promotion and transaction code have not been changed by writing this document.

Parent: [reliability and polish plan](reliability-polish-plan.md). This covers source identity, complete change transport, safe integration, and crash recovery.

## Outcome and boundaries

The integrated repository contains the complete change set that Tulid verified. A missing worker path declaration cannot silently omit a needed file, and deletions/renames are first-class delivery operations. Code, commit result, artifacts, task state, and board state reach one recoverable accepted outcome before dependents execute.

Configured `<workers>` propose implementation or review output through the existing completion protocol. Tulid derives the actual changes independently of the worker's tool, provider, or model. A submitted file list can help explain intent but is not the authority for transport.

Do not reintroduce predicted file allowlists, create a new merge service, force-reset user work, or expand parallel execution. Extend snapshots, jobs, and the existing transaction journal.

## Starting evidence and code

- `runtime/workspaces.py` excludes `.git` from worker copies.
- `runtime/verifier.py`: `_git_changed_files` cannot validate a diff without Git metadata; global reports currently contain empty changes and repeat the baseline digest.
- `runtime/completion.py`: `_changed_file_plan` copies only submitted paths whose source is still a file. Deleted paths fail verifier existence checks.
- `runtime/repository_facts.py`: baseline manifests, source inspection, and broad excluded directory names.
- `runtime/transactions.py`, `runtime/operation_log.py`, `runtime/completion.py`: effect application, compensation, journal recovery, and commits.
- `runtime/scheduler.py`, `runtime/resources.py`, `runtime/jobs.py`: serial execution and resource admission to extend across projects sharing a repository.

## Shared interfaces and invariants

This plan owns the source objects used by [plan 4](reliability-04-verification-plan.md):

- **Baseline manifest:** deliverable paths and their content digests, file kinds, modes, and supported link targets; source repository identity/revision and snapshot-rule version.
- **Candidate:** a sealed source snapshot, its manifest/digest, baseline reference, originating job/attempt/submission, and an authoritative complete change set.
- **Change entry:** add/edit/delete or a rename represented safely as delete+add, with before/after identities and modes. Rename detection is explanatory and cannot determine whether bytes are transported.
- **Acceptance record:** candidate digest, verification report/policy/image identities, integrated repository/commit identity, transaction ID, and final task outcome.

Internal field names are proposed interfaces to implement in existing runtime types, not a new worker-authored contract. A verification report is applicable only to its exact candidate. A task cannot be marked accepted before durable integration and final-state validation succeed.

## Implementation sequence

### 5A. Define the deliverable snapshot surface

Audit exclusions before changing comparison logic. A directory named `build`, `output`, or `dist` can contain intentionally versioned source; a blanket name exclusion must not discard it. For Git projects, include tracked deliverables even when they match a cache/output name, and include permitted nonignored new files from the worker. Exclude Tulid's own internal workspace files and known ephemeral dependencies/caches by explicit snapshot rules.

For non-Git projects, use documented project snapshot rules consistently at baseline, candidate, verification, and promotion. No later stage may reinterpret an excluded file as delivered. This is source transport policy, not a task-specific prediction of writable paths.

Handle binary files, empty files, executable bits, hidden files, directories changing into files, and supported symlinks. Never follow a symlink outside the source root when copying/deleting. Reject unsupported filesystem entries with a precise diagnostic rather than silently skipping them.

### 5B. Capture a stable candidate at submission

After authenticating/deduplicating a completion submission, capture the candidate in Tulid-owned storage outside the active worker mount. Quiesce worker writes or use a copy plus stable before/after manifest checks; do not assume copying a live directory is atomic. If a stable snapshot cannot be obtained, reject with a retriable submission/candidate error and preserve the workspace.

Seal the candidate against later worker mutation. Compute the baseline-to-candidate delta without relying on `changed_files`. Include added, edited, removed paths and modes. A stale/incomplete submitted list can produce a discrepancy note; it cannot cause silent partial transport. Continue enforcing path containment and artifact validity.

Support a genuine no-change candidate for review and other workflow transitions that permit it. If the workflow requires a change, determine that from the authoritative delta rather than from whether the worker supplied a nonempty list.

Expose candidate identity to verification and retain it through failure/retry. Plan 1 can preserve it as failed work; a repair produces a new candidate and invalidates the old verification result for the repaired bytes.

### 5C. Acquire the repository lane and check the target

Use a canonical repository identity, not project name, for the existing serial lane. Resolve alternate configured paths to the same repository consistently. Two projects pointing at one repository must not integrate concurrently, even when their worker model resources differ.

At integration, compare the target baseline/branch identity and deliverable manifest with those captured for the job. If source changed since the baseline, preserve the candidate and report stale/conflict. Do not copy selected files over a newer repository and continue using evidence from the old baseline.

For Git-backed automatic commits, require an integration target whose staged/working-tree state is understood. The first hardened path should use a clean managed checkout. If live user changes are present or overlap, stop integration with evidence rather than absorbing them into an automated commit. Never clean the user's checkout to satisfy this precondition.

Repository locking coordinates Tulid processes; it does not lock out arbitrary editors. Recheck target identities before mutation and use expected-before identities per effect. Do not claim a multi-file filesystem update is globally atomic. Conflicts and crash recovery must remain explicit.

### 5D. Promote through a durable acceptance transaction

Prepare the complete journal before effects. Record expected previous task/board state, target identities, candidate/report identities, each write/delete, necessary before-images, artifact destinations, and expected commit outcome.

Apply source operations from the sealed candidate, including deletions. Create or validate the Git commit through the existing commit path with a recorded parent/transaction identity. Do not stage unrelated paths. Treat an already applied effect with the expected after-identity as success, not another write/commit.

Validate the integrated deliverable manifest against the verified candidate. If the target baseline changed and the system prepares a new integrated candidate, it must go through verification again; it cannot reuse the old report by assertion.

Only then finalize artifact links, task/board transition, acceptance evidence, and dependent eligibility through the recoverable journal. Expose an incomplete transaction as pending/recovery-needed, never verified success. Release the repository lane only after acceptance or a safely settled failure.

### 5E. Recover without losing user work

For each prepared/failed journal, inspect expected-before and expected-after identities. Roll forward operations that can be proven safe; compensate only when the current file still matches the transaction's own after-image. An intervening user change must block compensation rather than be overwritten by a backup.

Handle a crash after commit but before recording its result by locating and verifying the already-created transaction commit. Do not create a duplicate commit or reset branch history blindly. Handle a crash after task write but before board write by completing/reconciling the same intended state.

Recovery must be repeatable and result in one committed acceptance record or an explicit unresolved conflict with all candidate/before-image data retained. Retention cleanup runs only after terminal settlement and must not delete the sole recoverable failed patch.

### 5F. Feed reliable evidence to review and scheduling

Store actual added/edited/deleted paths, source identities, check evidence, and commit/transaction IDs on accepted completion. Plan 2 supplies that evidence to review. Plan 6 uses it to distinguish verified task success from a manual state change.

A dependent job is admitted against the accepted repository identity, not merely an updated board column. If a previous acceptance transaction is unresolved, the repository lane remains unavailable with an actionable reason.

## Fault tests and acceptance

Extend `tests/runtime/test_repository_facts.py`, `test_workspaces.py`, `test_completion.py`, `test_transactions.py`, `test_jobs_scheduler.py`, and scripted end-to-end fixtures.

| Scenario | Required behavior |
| --- | --- |
| Worker omits a required added file from its list | Full candidate change set still reaches the repository, or completion is explicitly rejected; never partial acceptance. |
| Delete/rename/binary/mode-only change | Correct bytes, absences, and modes survive verification/promotion. |
| Tracked source under a broadly excluded directory name | Deliverable is retained and compared. |
| Symlink escaping the root or unsupported entry | Explicit failure without modifying outside paths. |
| Worker writes while candidate capture runs | Stable snapshot or bounded capture rejection, not mixed bytes. |
| Worker changes files after checks pass | Immutable verified candidate is promoted; later workspace edits cannot ride along. |
| User edits target after job baseline | Stale/conflict outcome, preserved work, no overwrite or implicit commit of user changes. |
| Two configured projects share one repository | One integration lane and no overlapping acceptance. |
| Crash before/after every write, delete, commit, task/board effect | Repeated recovery reaches one consistent result or a precise conflict. |
| Completion/event replay after acceptance | No duplicate task movement, artifact, or commit. |
| Review produces no changes | Accepted when workflow allows it, with valid unchanged candidate evidence. |

Run final checks on a fresh checkout of the resulting commit, not just the worker directory. Compare its deliverable manifest with the verified candidate before claiming successful transport.

## Dependencies, migration, and completion evidence

Implement 5A–5B first and expose fixtures/interfaces to plan 4. Then integrate the verifier's exact-candidate report before completing 5C–5F. This is a staged dependency, not a circular requirement to finish both plans at once. Use plan 1's attempt identity and plan 3's task revision.

Version manifests and journals. Retain readers for historical records but do not claim exact-candidate delivery for old acceptances that lack evidence. Test migration/recovery on a repository and tracker copy. Avoid changing snapshot rules in the middle of an admitted job.

Deliver manifest/change-set examples, deletion/omitted-file regressions, repository conflict tests, and a crash-injection recovery matrix. Completion requires the accepted repository and tracker to match the verified candidate and intended workflow outcome, with no lost changes or dependent execution on partial acceptance.
