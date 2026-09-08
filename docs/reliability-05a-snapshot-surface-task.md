# Task 5A — Define the deliverable snapshot surface

Parent: [reliability-05-delivery-plan.md](reliability-05-delivery-plan.md), plan 5 step 5A.
Status: task definition for the reliability polish effort. This document defines WHAT the
deliverable snapshot surface must be, so an implementation worker has the observed behaviors,
settled decisions, prerequisite code, and acceptance evidence. It does not predict writable
paths, author per-file allowlists, or add a new worker-authored contract.

## Why

Tulid must transport exactly the changes it verified. Today the boundary between "deliverable
source" and "ephemeral/internal files" is decided by a fixed directory-name exclusion set
(`EXCLUDED_DIRECTORY_NAMES` in `runtime/repository_facts.py`). That set is applied verbatim
everywhere: baseline capture, workspace copies, and top-level entry inspection. A directory
named `build`, `output`, or `dist` is dropped even when it contains intentionally versioned
source, and Tulid's own internal workspace files are excluded only because `.open-tulid`
happens to match a name, not because a rule says so. Because the rule set is the same list used
for transport, there is no way to express "tracked source at a cache-like name is still
deliverable."

This task defines a single, explicit deliverable snapshot surface that:

- keeps comparison and transport honest regardless of file/directory *names*;
- is applied consistently at baseline, candidate, verification, and promotion, so no later
  stage can reinterpret an excluded file as delivered;
- still handles binary/empty/executable/hidden files, directories changing into files, and
  supported symlinks, and rejects unsupported filesystem entries with a precise diagnostic.

5A is a definition task; implementing the rules in code is the worker's job under the prose
below. 5B (capture) and 5C–5F (lane, transaction, recovery) consume this surface. Keep this
step scoped to the snapshot surface only.

## Observable behavior that must change

| Requested behavior | Current behavior (gap) | Required after 5A |
| --- | --- | --- |
| A blanket name exclusion must not discard tracked/versioned source. | `build`, `output`, `dist` are dropped by name in `repository_facts._repository_files`, `_copy_repo`, and `top_level_entries`. | A tracked deliverable under a cache/output-like name is retained, compared, and promotable. |
| Git projects: tracked deliverables always included even if the name matches a cache/output name. | Git status is not consulted to rescue a tracked file under an excluded name. | Tracked paths are deliverable regardless of directory name. |
| Git projects: permitted non-ignored new files from the worker are included. | Only `submission.changed_files` are considered for promotion (in `completion._changed_file_plan`); new non-ignored files outside that list are not derived from the repository. | Non-ignored new files on the candidate are part of the deliverable delta. |
| Exclude Tulid's own internal workspace files and known ephemeral dependencies/caches by explicit snapshot rules. | Exclusions are a name list; `.open-tulid` and caches are dropped only by name coincidence. | Internal/`.open-tulid` files and caches are excluded by explicit, documented snapshot rules. |
| Non-Git projects: documented snapshot rules are used consistently at baseline, candidate, verification, promotion. | The same name list is applied by walking the tree; nothing documents that the non-Git rule set is authoritative. | One documented rule set governs all four stages for non-Git projects; no stage reinstates an excluded file as delivered. |
| Binary, empty, executable, hidden files survive comparison and transport. | `FileManifestEntry` records path/sha256/size only; there is no mode or file-kind field, so executable bits are not tracked or carried reliably. | Manifest entries carry file kind, mode, and supported link targets; executable/empty/binary/hidden files are included, compared, and transported. |
| Directories changing into files are handled. | `_repository_files` yields only `path.is_file()` paths, so the type change is invisible today and the manifest cannot express a dir→file transition. | A path type change produces a correct delete+add in the derived delta, not a silent skip. |
| Supported symlinks preserved; a symlink escaping the source root is never followed. | Workspace copies use `shutil` behavior; verifier has a `_escapes_via_symlink` guard for artifacts only, not for deliverable transport. | Copying/deleting never follows a symlink outside the source root; supported links carry their target; escaping links are rejected. |
| Unsupported filesystem entries are rejected with a precise diagnostic, not skipped silently. | The walk lifts only regular files; sockets/fifos/devices are silently absent. | Unsupported entries produce a precise, actionable error rather than silent omission. |

## Reconcile outdated mechanisms with the current workflow

Keep the task prose separate from the project-wide verification commands (they are in the
"Project verification commands" section). The following existing mechanisms must be reconciled
so 5A's rules are the single authority:

- `verifier._git_changed_files` compares `submission.changed_files` against `git status` inside
  the worker workspace, but `workspaces._copy_repo` excludes `.git` from worker copies, so the
  diff is empty and this check cannot independently validate changes. 5A must define the
  deliverable surface so the authoritative delta comes from baseline→candidate manifests, not
  from a `.git` directory that the worker workspace deliberately omits.
- `completion._changed_file_plan` copies only submitted paths and only while the source is still
  a file; deletions and renames are invisible to transport. 5A defines the surface (and 5B
  computes the authoritative delta) so a missing submitted path cannot silently omit a needed
  file.
- `repository_facts` mixes scanning, facts, and baseline in one module; 5A should keep those
  responsibilities but introduce a versioned set of snapshot rules (baseline-manifest schema is
  already `tulid.baseline-manifest/v1`). Do not raise the schema version for unrelated reasons.

## Settled decisions and interfaces

These are decisions 5A makes. They are refinements of existing runtime types, not new worker
contracts.

- **Versioned snapshot rules.** Table the current `EXCLUDED_DIRECTORY_NAMES` into an explicit,
  versioned snapshot-rule set (internal to Tulid, mirrored by a `snapshot-rule version` field on
  the baseline manifest and repository facts). Rules distinguish:
  - Tulid's own internal workspace files (the whole `.open-tulid` tree): always excluded;
  - known ephemeral dependencies/caches (e.g. `node_modules`, `.venv`, `__pycache__`, package
    caches): excluded by rule;
  - intentional source under a name like `build`/`output`/`dist`: **not** excluded by name.
- **Git-aware deliverable rule.** For Git repositories, a path is deliverable when it is a
  tracked path (whether or not the name matches a cache/output name) or a permitted non-ignored
  new file present on the candidate. Ignored and internal files are not deliverables. The worker
  workspace may omit `.git`; the rule must still be expressible and verifiable from the
  candidate surface itself.
- **Non-Git single rule set.** For non-Git projects, the documented snapshot rules are
  authoritative and identical at baseline, candidate, verification, and promotion.
- **File kinds and modes.** Extend the manifest entry to record file kind (regular file vs
  supported symlink), mode (including executable bits), and supported link target; keep the
  existing path/sha256/size fields. Binary and empty files are hashed and compared normally;
  empty is not an error.
- **Type-change delta.** A directory becoming a file (or the reverse) is represented safely as
  delete+add of that path. Rename detection is explanatory only and never determines transport.
- **Symlink containment.** Copying and deleting must never follow a symlink outside the source
  root. A symlink escaping the root, or any unsupported filesystem entry, is rejected with a
  precise diagnostic (a `DomainError` with a stable code) rather than silently skipped.
- **No new file allowlists.** These are source-transport policy rules, not a task-specific
  prediction of writable paths. Do not reintroduce per-task `accepts:`/predicted-path
  acceptance.

Workers have discretion over implementation details: data structure shape, helper placement, and
which existing types (e.g. `BaselineManifest`, `FileManifestEntry`, `RepositorySnapshot`) gain
fields. The acceptable changes per behavior are in the evidence table below.

## Existing code and prerequisite work

- `src/open_tulid/runtime/repository_facts.py` — `EXCLUDED_DIRECTORY_NAMES`, `FileManifestEntry`,
  `BaselineManifest`, `capture_repository_snapshot`, `_repository_files` (name-based pruning),
  `baseline_manifest_to_dict`, `repository_facts_to_dict`.
- `src/open_tulid/runtime/workspaces.py` — `_copy_repo` (copies via `EXCLUDED_DIRECTORY_NAMES`),
  workspace baseline mismatch check, `.open-tulid` context writing, frozen context files.
- `src/open_tulid/runtime/verifier.py` — `_git_changed_files`, `_manifest_changes`,
  `_escapes_via_symlink`, contract-checks runner (the only acceptance criterion is the global
  command policy).
- `src/open_tulid/runtime/completion.py` — `_changed_file_plan` (copies only submitted file
  paths), `_commit_plan`, `_same_file_content`.
- `src/open_tulid/runtime/baseline.py` — runtime baseline capture; the `RepositorySnapshot`/
  `BaselineManifest` identity is the comparison unit 5B will reuse.
- Prerequisite: baseline and schema consistency belong to plan 3 (task quality); 5A does not
  rework the task schema. Retain readers for historical records; old baselines keep their
  meanings.

## Behavior → evidence mapping

Use this during the planning pass to tie each requested behavior to the implementing task and
the expected evidence.

| Requested behavior (5A) | Implementing task | Expected evidence |
| --- | --- | --- |
| No blanket name exclusion discards tracked/versioned source | Snapshot rules make exclusion rule-based, not name-list-based, for deliverable candidates | A file under `build/` or `output/` included as tracked/versioned appears in the baseline manifest; a name-only exclusion test no longer drops it. |
| Git: tracked deliverable under cache-like name retained and compared | Git-aware deliverable rule | Fixture with tracked `dist/x` and an untracked cache blob → baseline contains `dist/x`, omitted cache file stays out. |
| Git: permitted non-ignored new worker file in the delta | Include non-ignored new files from the candidate | New non-ignored file appears in the candidate manifest/delta equal to a real change, not requiring a submitted list entry. |
| `.open-tulid` and ephemeral caches excluded by explicit rules | Explicit snapshot rule entries | Baseline/workspace copy excludes `.open-tulid`, `.venv`, `__pycache__` by rule; a test names these explicitly. |
| Non-Git: one rule set across baseline/candidate/verification/promotion | Documented non-Git snapshot rule set | Same rules honored at all stages; a test asserts an excluded path is excluded at every stage and never resurfaces as delivered. |
| Binary, empty, executable, hidden files compared/transported | Kinds + modes + file-kind entries | Empty and binary fixtures hash/compare; executable bit present in manifest after capture and after transport; hidden file included. |
| Directory↔file transitions | delete+add representation | Dir→file change yields delete+add of that path in the delta; no silent skip. |
| Symlink containment | Containment + precise rejection | Escaping/unsupported symlink → explicit `DomainError` code; no outside path written or deleted. |

## Acceptance — what demonstrates success

A completed 5A is demonstrated when the snapshot surface it defines:

1. Retains and compares tracked/versioned source under cache/output-like names (no name-based
   discard).
2. Includes permitted non-ignored new files for Git projects; excludes `.open-tulid` and known
   ephemeral caches by explicit rules.
3. Applies one documented non-Git rule set identically at baseline, candidate, verification, and
   promotion, such that no excluded file is later delivered.
4. Handles binary, empty, executable, and hidden files; represents directory↔file changes as
   delete+add.
5. Preserves supported symlinks, never follows a symlink outside the source root, and rejects
   unsupported filesystem entries with a precise diagnostic rather than silently skipping them.

These are definition-level acceptances; 5B–5F turn them into end-to-end transport regressions.
5A is complete only when the definition is committed and the project verification commands pass.

## Project verification commands

These are the project-wide commands for the Tulid repository, defined once and reused for every
implementation step; they are not per-task commands:

```sh
.venv/bin/python -m pytest -q --ignore=tests/e2e
```

Relevant regression areas for this task (run and report observed results):
`tests/runtime/test_repository_facts.py`, `tests/runtime/test_workspaces.py`, and the
snapshot/delta/enforcement regions of `tests/runtime/test_completion.py` (the
`DeterministicVerifier` tests).
