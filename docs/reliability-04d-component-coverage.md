# Reliability 4D implementation and evidence

Tulid's global verification execution and the staged Wealthy Scholar coverage
entry point are implemented. **The Wealthy Scholar application migration is not
installed and its clean-image baseline is not green.** Application source was explicitly excluded from changes. The tracker migration
is staged here because activating it before its required setup exists would not
establish coverage.
The exact missing setup and the admission sequence are documented in
[the project-owned migration](../examples/wealthy-scholar-verification/README.md).
No worker, scheduler, proxy, Docker runtime/container, or model service was
started or stopped for this work. Only isolated deterministic test doubles and
local test subprocesses were exercised.

## Root causes

1. The existing project global policy ran only `npm test --prefix backend`.
   Actual manifests provide no research package/lock, frontend lock/tests,
   dedicated Node contract suite, or deterministic integrated suite. Seventeen
   backend test files exist; some Mongo-dependent tests skip without Mongo.
   A successful backend command cannot establish the promised product coverage.
2. The earlier 4B executor and writable-copy helpers were not connected to
   `CompletionService`. It captured a candidate but verified the mutable worker
   workspace, defaulted to host subprocesses, and never bound reports to a
   resolved worker image. Component commands could therefore run with the wrong
   dependencies and against different source bytes.
3. The verifier and candidate used different snapshot rules. The former ignored
   deliverable source under `build`, `dist`, and `output`. Lock identity scanning
   included installed dependency manifests while omitting common Node locks.
   Installing dependencies could create false mutation failures, and changes to
   deliverable source could evade detection.
4. Verification container names depended only on the command name, permitting
   collisions across jobs and invalid Docker names. Docker startup failures were
   not consistently environment blockers. Output was truncated before any full
   log was retained, despite reports promising complete evidence.
5. Empty global command lists passed parsing, and the `ExecutionContract.commands`
   projection silently reset nonzero exit expectations to zero.
6. The pre-existing uncommitted automation tests inspected real OpenCode processes
   despite using fake workers. An unrelated live process caused two timeouts and
   one failed reset. The subprocess test harness now mocks that process scan;
   the production shared-model guard remains intact.

## Architecture and boundaries

The project keeps its one ordered `contract.yaml`. No task commands, task file
allowlists, component fields in Tulid's schema, or separate test platform were
introduced. Implementation and review use the same frozen command policy.
The project-owned entry point uses actual Node TAP, pytest/JUnit, and frontend
build outputs to reject missing/empty/skipped discovery and disabled builds.
Its declared expectations include backend, Node/Python contracts, research,
frontend, and deterministic integration. New matching tests are discovered
without editing tasks; additional components extend the same project entry point.
No listed component is implicitly deferred.

Before worker launch, Tulid resolves the configured local project image to its
immutable ID, uses it for that worker, and persists the sanitized verification
environment with the job. Completion and CLI replay consume that environment,
run global commands in a writable copy of the sealed candidate, and retain
complete logs with bounded report excerpts. Missing environment evidence fails
closed. Global-contract promotion reads submitted source paths from the sealed
candidate, preventing a late worker edit from replacing verified bytes. Complete
manifest-driven transport/deletions remain the responsibility of later plan-5
steps; this change does not claim those steps are complete.

Candidate capture, mutation detection, and lock scanning now share the same
deliverable traversal. Dependencies/caches and `.open-tulid` may change; source,
configuration, manifests, and locks may not. Build outputs in the staged entry
point are explicitly directed under `.open-tulid`. Required globals still all
run in order, and later success cannot hide an earlier failure.

## Files

Runtime changes:

- `src/open_tulid/runtime/candidate.py`: shared deliverable traversal/digest API.
- `src/open_tulid/runtime/completion.py`: candidate copy, frozen environment,
  container execution, sealed source for existing promotion path.
- `src/open_tulid/runtime/executor.py`: resolve and persist the worker/verifier image.
- `src/open_tulid/runtime/jobs.py`: immutable verification environment metadata.
- `src/open_tulid/runtime/verification_runtime.py`: correct lock surface, unique
  containers, environment failure classification, full output, image resolution.
- `src/open_tulid/runtime/verifier.py`: fail-closed default, shared source digest,
  image-bound request/report and retained logs.
- `src/open_tulid/runtime/standard_contracts.py`: reject empty global policies.
- `src/open_tulid/runtime/execution_contracts.py`: retain exit expectations.

Project-owned migration and documentation:

- `examples/wealthy-scholar-verification/contract.yaml`
- `examples/wealthy-scholar-verification/tools/verify_project.py`
- `examples/wealthy-scholar-verification/README.md`
- `docs/implementation-contracts.md`
- `docs/reliability-04d-component-coverage.md`

Regression changes:

- `tests/runtime/test_project_components.py`
- `tests/fixtures/project_components/tool.py`
- `tests/runtime/test_completion.py`
- `tests/runtime/test_executor.py`
- `tests/runtime/test_execution_contracts.py`
- `tests/runtime/test_standard_contracts.py`
- `tests/runtime/test_verification_runtime.py`
- `tests/runtime/test_verifier.py`
- `tests/e2e/test_standard_contract_e2e.py`

Previously uncommitted automation is preserved separately, including
`reliability_auto_loop.py`, `reliability_auto_config.json`, and
`docs/reliability-auto-loop.md`. Its `tests/test_reliability_auto_loop.py` now uses
an isolated subprocess harness. Runtime state files and the existing deletion of
`CODEX-HANDOFF.md` are not part of this implementation.

## Verification

Before changes:

- Focused runtime suite: **138 passed**.
- `.venv/bin/python -m pytest -q --ignore=tests/e2e`: **805 passed, 3 failed**.
  All three failures were real-process interference in the automation tests.

After changes:

- `.venv/bin/python -m pytest -q tests/runtime/test_standard_contracts.py tests/runtime/test_execution_contracts.py tests/runtime/test_verification_runtime.py tests/runtime/test_verifier.py tests/runtime/test_completion.py tests/runtime/test_executor.py tests/runtime/test_project_components.py tests/test_reliability_auto_loop.py`: **187 passed**.
- `.venv/bin/python -m pytest -q --ignore=tests/e2e`: **849 passed**, two existing
  multiprocessing/fork deprecation warnings.
- Docker E2E assertions updated and syntax checked, **not run** because they
  start runtime/container services. No Docker or real-provider pass is claimed.
- Read-only inventory against the actual application reproduces all missing
  setup/discovery blockers in the migration README. It executed no application code.

Deliberate failures cover each promised component, empty and skipped tests,
missing directories, a shrinking backend discovery floor, disabled frontend
builds, newly added tests, and an additional declared component. Scripted-toolchain
checks exercise the real parser/compiler/verifier; a real local Node check proves
empty-file and skipped-test handling. Real completion/executor regressions cover
image persistence, container request construction, late edits, failed-middle-command
settlement, logs, and idempotent replay. The toolchain fixture is test-only and is
not included in the staged application setup.
