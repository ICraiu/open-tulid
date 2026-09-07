# Plan 4 — Reproducible verification of the actual product

Status: implementation plan; verifier behavior and project commands have not been changed by writing this document.

Parent: [reliability and polish plan](reliability-polish-plan.md). This covers trusted checks and global test coverage.

## Outcome and boundaries

Tulid independently runs the project global policy against a stable candidate in the project's declared toolchain. The result means the configured checks ran and passed for those exact source bytes. Host dependencies, worker summaries, or omitted suites cannot silently substitute for that evidence.

Configured `<workers>` may perform planning, implementation, review, and diagnostics with any supported tool/model. Verification is a Tulid-controlled execution step. It does not rely on which worker produced the code or on that worker claiming success.

Keep one project command policy inherited by every implementation and review task. Do not add per-task check selection, generated allowlists, or a separate test platform. Reuse project images, existing command records, and completion evidence.

## Starting evidence and code

- `runtime/verifier.py`: `_run_contract_checks` invokes host `subprocess.run` in the mutable worker workspace.
- `runtime/execution_contracts.py`: global checks are sorted by ID rather than preserving their declared order.
- `runtime/standard_contracts.py`: project command parsing, retry/runtime settings, and validation.
- `containers/project_images.py`, `containers/runtime.py`, `containers/service.py`: image construction and execution plumbing to reuse for non-agent verification.
- `runtime/completion.py`, `runtime/workspaces.py`, `runtime/repository_facts.py`: submission handling, source copies, and source identity.
- Wealthy Scholar's global contract currently runs only backend tests despite Python parity and frontend requirements. Its project Dockerfile adds no project-specific toolchain.

## Decisions and inputs

1. Plan 5 owns the authoritative immutable candidate and manifest. This verifier consumes that candidate, baseline identity, ordered global command policy, and exact project image identity.
2. Run verification in a separate disposable container, overriding any agent entrypoint. Match the project's worker toolchain/image, but pass no model-proxy or completion credentials. Use only dependencies/environment required for the checks.
3. Use a writable verification copy when commands create caches/build outputs. Keep the accepted candidate separately immutable. Compare deliverable source identities after verification; reject checks that mutate source/lockfiles to make themselves pass.
4. Preserve declared command order, run each required command, and require every expected exit status. A failed command cannot be hidden by a later passing command. Record explicit not-run outcomes if execution cannot continue due to an infrastructure failure.
5. Global policy remains project-authored. Tasks add tests and implementation; they do not author separate command contracts or select a smaller subset for acceptance.

## Implementation sequence

### 4A. Specify verifier request and report

Extend existing verification types with candidate/baseline digest, command-policy digest, project image identity, environment identity, and ordered command results. Each result includes argv, working directory, timeout, expected/actual exit status, timestamps/duration, and log references. Bound inline excerpts; retain complete logs as artifacts.

Record classification separately from pass/fail: malformed policy, unavailable tool/runtime, dependency preparation failure, command timeout, failed behavior check, source mutation, or infrastructure interruption. Do not equate an empty report or a command that never ran with success.

Maintain legacy report readers. New reports must not repeat the baseline digest as the candidate/post digest unless the actual source tree is unchanged.

### 4B. Freeze and execute in the declared environment

At valid completion submission, obtain the frozen candidate from plan 5. Prepare a verification copy/container using the same resolved image identity as the worker attempt. Resolve repository-relative paths within that copy. Do not execute submitted project code directly on the Tulid host.

Provision project dependencies from lockfiles using a documented project preparation step or image build. Distinguish toolchain provisioning from application tests. Capture the lockfile and environment identities and allow no install to silently rewrite committed locks.

Keep preparation noninteractive and independent of model credentials. Use configured networking only when dependency/service preparation needs it; deterministic tests must not call live model/providers accidentally. If a required dependency/service is unavailable, report the environment blocker before spending a model repair attempt.

Ensure cancellation/timeout terminates the verification process tree/container and records the interrupted command. Integrate with completion settlement from plan 1 so worker exit cannot tear down an otherwise valid verification outcome.

### 4C. Preserve global command semantics

Remove alphabetical sorting of checks. Compile command order from `contract.yaml` and include that order in the frozen policy identity. Validate argv/path/timeout definitions before worker admission using the existing parser.

Render exactly the same global list in implementation and review prompts. A worker's narrow diagnostic checks are supplementary. Acceptance always runs the global list in the verifier regardless of submitted validation prose.

Freeze the policy at job admission. An operator edit applies to future jobs/revisions, not silently to a current candidate. A candidate cannot edit a tracker contract through its application workspace to alter its own acceptance policy.

### 4D. Cover all promised project components

For Wealthy Scholar, inspect actual manifests and add a project-owned verification entry point or explicit ordered commands covering backend tests, Python contracts/parity, frontend build/tests, and deterministic integrated behavior. Verify discovery, not merely command names: the expected suites must actually execute.

Resolve the bootstrap problem before admitting dependent implementation tasks. Establish the required toolchain and test entry points as a small project setup change; then freeze the resulting global policy for the implementation run. A task introducing a new component must also wire its tests into the global entry point before it can pass.

Where the product plan intentionally defers a component, define that baseline explicitly in project-owned expectations. Once a component is introduced/required, deleting its directory or test discovery must fail. Do not implement a generic `if directory exists, skip otherwise` rule that turns missing delivered work green. These are global project expectations, not task-specific selectors.

Use deterministic provider/data fixtures for routine acceptance. Plan 6 separately validates the real integration behavior and existing UI journeys. Do not claim that backend unit tests establish frontend usability or Python parity.

### 4E. Return useful evidence and guard coverage regressions

Return command-specific output to the worker through existing completion feedback. Distinguish a failed assertion from a missing interpreter, service, or lockfile. Attach a stable evidence reference so retries/review see the same failure without copying entire noisy logs into the prompt.

Compare the candidate's test-discovery/build-script changes with the baseline in review. Legitimate test refactors remain allowed, but disabling suites cannot be accepted merely because a command exits zero. Use project-owned discovery assertions where practical; semantic test-quality assessment stays with the existing review process in plan 6.

Recompute the deliverable source digest after checks. Cache/build artifacts excluded by the declared snapshot rules may change; tracked source, tests, configuration, schemas, and lockfiles may not be altered by verification unnoticed. Promotion consumes the original immutable candidate, never arbitrary post-test workspace contents.

## Regression and acceptance checks

| Case | Expected result |
| --- | --- |
| Host lacks Node/Python while the project image has them | Candidate verifies normally; no host executable dependency. |
| Host has a different runtime version | Identical image/candidate yields identical execution environment and results. |
| Required command missing, bad cwd, timeout, failed install | Specific environment/policy outcome with no fabricated passing checks. |
| Commands named `z_setup` then `a_tests` in the contract | Declared order is preserved end to end. |
| Worker claims success without running tests | Verifier still runs every global command. |
| Worker edits files after submitting | Frozen candidate/result do not change. |
| Test mutates source, schema, or lockfile | Verification rejects source mutation and retains evidence. |
| Broken Python parity, backend assertion, or frontend build | Each independently fails the global policy. |
| Required suite is missing or discovery is disabled | Project coverage expectation fails or review blocks the change; it cannot count as demonstrated coverage. |
| Interrupted verifier and completion replay | One authoritative result/acceptance, no duplicate promotion. |

Extend `tests/runtime/test_standard_contracts.py`, `test_execution_contracts.py`, `test_completion.py`, `test_executor.py`, verifier tests, and container/project-image tests. Add scripted Docker coverage to `tests/e2e/test_standard_contract_e2e.py`; run it separately from the deterministic suite and report skips accurately.

## Dependencies, migration, and completion evidence

Agree candidate/report types with plan 5 before implementation. Land its snapshot foundation before integrating this verifier. Command-order and pure request/report tests can land earlier. Error categories and settlement use plan 1; prompt rendering uses plan 2.

Record the old/new effective policy and image requirements for a project migration. Verify the baseline in the new environment first, and explain existing failures before asking a worker to change application behavior. Retain historical reports under their original schema and do not upgrade them into stronger evidence retroactively.

Deliver a clean-environment verification trace, deliberate failure tests for each project component, source-mutation protection, and evidence that implementation/review inherit the same global commands. Completion requires reproducible outcomes from a clean candidate independent of host tool installations.
