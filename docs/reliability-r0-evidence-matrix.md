# Reliability R0 — exact handoff and evidence matrix

Prepared: 2026-09-11. This document is the accounting record for package **R0** of
the reliability remaining-work plan (`docs/reliability-remaining-work-plan.md`).
It records the source identity, dirty working-tree, installed runtime, retained
test-run evidence, and a per-step status for the original 32 plan steps. It does
not claim any new product or worker proof; it is an accounting artifact.

## 1. Source identity snapshot (sanitized)

| Item | Value |
| --- | --- |
| Working directory | `/home/rawsteel/repo/open-tulid` |
| Branch | `master` |
| HEAD commit | `990247d3dbc8252001561b69c0e792495209ddfe` |
| HEAD short | `990247d` |
| HEAD subject | `fix reliability 2B: block admission when parent context is unavailable` |
| HEAD tree | `5d21706288a5aecc400c784c92591246e06104bd` |
| Ahead of `origin/master` | 32 commits |
| Semantic task revision / workflow identity | captured at runtime as `runtime-baseline.json` per `runtime/baseline.py`; see §3 |

All cited code references below use the working tree at this HEAD plus the
uncommitted edits recorded in §4. No final-tree test run exists in the retained
evidence; that is an R9/global-gate responsibility, not something R0 claims.

## 2. Dirty working-tree (pre-existing user state)

The following tracked files are modified and **uncommitted** at R0 time. They
are pre-existing user/loop state and are preserved, not authored or owned by R0:

| File | Diff digest (sha256 first 12) |
| --- | --- |
| `docs/reliability-auto-loop.md` | `552050ec55b0` |
| `reliability_auto_config.json` | `81c7c93c13c7` |
| `reliability_auto_loop.py` | `7abf88cb3b03` |
| `tests/test_reliability_auto_loop.py` | `152861670ce8` |
| `src/open_tulid/domain/completion.py` (R1 edit) | `fb7a8a4855d5` |
| `src/open_tulid/runtime/scheduler.py` (R1 edit) | `a0aea8536520` |

Whole dirty diff digest: `b6649c83f70e`. The two files marked **R1 edit** are the
terminal-outcome edits owned by R1 and are recorded separately in §4; the other
four are auto-loop automation files preserved as-is.

Untracked files present (pre-existing loop state, not authoritative evidence of
this plan's progress, per remaining-work-plan §1): `.reliability-auto-state.json`,
`.reliability-auto-state.json.lock`, `.reliability-loop-state.json`,
`.reliability-remaining-state.json`, `.reliability-remaining-state.json.lock`,
`docs/reliability-remaining-work-plan.md`.

## 3. Installed Tulid executable/module location

Verified directory, not assumed:

| Item | Path | Relationship to checkout |
| --- | --- | --- |
| CLI wrapper | `/home/rawsteel/.local/bin/tulid` → `/home/rawsteel/.local/share/uv/tools/open-tulid/bin/tulid` | Symlink into the uv-tools virtualenv |
| uv-tools virtualenv | `/home/rawsteel/.local/share/uv/tools/open-tulid/` | Contains `_editable_impl` `.pth` |
| Editable source pointer | `.../open-tulid/_editable_impl_open_tulid.pth` → `/home/rawsteel/repo/open-tulid/src` | Installed runtime resolves to the checkout being edited |
| `.venv` editable pointer | `/home/rawsteel/repo/open-tulid/.venv/lib/python3.14/site-packages/_editable_impl_open_tulid.pth` → `/home/rawsteel/repo/open-tulid/src` | Test interpreter resolves to the same checkout |
| Module entry | `import open_tulid` → `/home/rawsteel/repo/open-tulid/src/open_tulid/__init__.py` | Editable install |

Conclusion: the installed runtime and the test interpreter both point into the
current checkout (`/home/rawsteel/repo/open-tulid/src`). This is verified via the
`.pth` files, not assumed. `tulid --version` is not a supported option (CLI exits
with "No such option").

## 4. Uncommitted R1 edits (recorded separately)

These two files carry the unfinished R1 terminal-outcome change from the handoff
(remaining-work-plan §1). They are `unverified`: no retained test result exists
for them (the requested test command was rejected by approval review per the
handoff). They are **not** treated as accepted.

`src/open_tulid/domain/completion.py` (blob `b50b9db962095862f67be6b6f8b21312b416b39`):
- `LEGACY_AMBIGUOUS_OUTCOME` changed from implicit success to `"ambiguous"`.
- `dependency_outcome` docstring: `success` requires a declared success terminal;
  `ambiguous` is returned when no terminal outcome was declared, and migration is
  required before the dependency can release work.

`src/open_tulid/runtime/scheduler.py` (blob `9180e0180d47ae1f26f7ec7243e232fd09578c6a`):
- `_dependency_error` now returns `task.dependency_outcome_ambiguous` with a
  diagnostic naming the state when `dependency_outcome` returns `"ambiguous"`.

Owner: **R1** ("truthful dependency admission") validates and completes or
corrects these edits. R0 records them; it does not validate them.

## 5. Full-suite timing distinction (preserved)

Two retained run logs exist under `.reliability-proof/20260911/` (Git-ignored,
preserve across restarts).

| Log | Result | mtime | Source identity | Final-tree pass? |
| --- | --- | --- | --- | --- |
| `full-suite.log` | **997 passed**, 2 multiprocessing warnings, 126.33 s | 2026-09-11 08:11 | After `a2575d3` per remaining-work-plan §1; predates commits `94e21ee`, `1fcf077`, `efe7e5a`, `990247d` and both uncommitted R1 edits | **No** |
| `deterministic-suite.log` | **1 failed, 968 passed**, 2 warnings, 47.23 s | 2026-09-11 08:01 | Separate run; failure `test_operator_cli.py::test_runtime_stop_refuses_to_claim_success_when_scheduler_does_not_exit`; no exact recorded commit | **No** |

Per remaining-work-plan §1, the full-suite run predates the subsequent harness,
unsupported-entry, artifact-collision, and missing-parent fixes. It is **not** a
full-suite result for the final working tree, and it is **not** inferred as such
anywhere in this matrix. No retained log establishes a final-tree pass.

Other retained evidence (all *.log under `.reliability-proof/20260911/`):
`image-build.log`, `model-proxy.log`, `task7-preview.log`. The private-Mongo
226-test baseline log was lost during a host restart and is **not** cited as a
retained artifact (remaining-work-plan §1); reproducing it is an R7 acceptance
case, not an R0 claim.

## 6. Original 32-step matrix

Status keys: **A** = accepted; **P** = partial; **U** = unverified; **EB** =
externally blocked. `Owner` names the R-package that closes any gap. Citations
are to source/test files under `/home/rawsteel/repo/open-tulid`. "Present" means
code and tests were inspected to exist and exercise the behavior; it is not a
final-tree test pass claim (see §5).

### Plan 1 — Reliable worker execution and recovery

| Step | Status | Concrete criterion | Code reference | Test / run evidence | Gap owner |
| --- | --- | --- | --- | --- | --- |
| 1A Pin baseline + define attempt record | A | Versioned attempt record (task revision, transition, attempt#, dates, worker, status, failure ref); baseline captures source revision/dirty patch/installed location/policy/image; admission persisted under lease before spawn; restart cannot duplicate budget | `runtime/attempts.py:AttemptRecord:50`, `task_semantic_revision:79`, `reconcile_attempt_records:198`; `runtime/baseline.py:capture_runtime_baseline:42`; `runtime/executor.py:_admit_attempt:686`; `runtime/jobs.py:_record_attempt_unlocked:235` | `tests/runtime/test_attempts.py::test_semantic_revision_*record`, ::test_store_persists_attempt_record_across_restart, ::test_settle_interrupted_attempts_*; commits `94bc77f`, `9afe333`, `8489800` | R5 (historical/legacy accounting) |
| 1B Correct session creation + rejection evidence | A | Explicit attempt expiry through both session stores; injectable clock; typed session lookup (valid/expired/unknown); revoking old attempt cannot revoke new credential; log timestamp/reason/HTTP without tokens | `runtime/model_proxy.py:ModelProxySession.issue:72`, `ModelProxySessionStore.get`, `revoke_attempt:109`; `runtime/executor.py:_model_proxy_env:963` | `tests/runtime/test_model_proxy.py::test_auth_expiry_with_fake_time_*, ::test_revoke_attempt_does_not_revoke_newly_issued_credential, ::test_model_proxy_logs_rejection_evidence_without_tokens`; commits `14f2986` | none |
| 1C Classify failures at executor boundary | A | Generic result (code/category/retryable/retry_action/evidence/path); read proxy evidence → tool events → log signatures; doom-loop only from explicit signature; exit-without-completion distinct from nonzero | `runtime/failures.py:ExecutionFailure:23`, `classify_worker_failure:153`, `_DOOM_LOOP_SIGNATURES:53`; `runtime/executor.py:1147,1178` | `tests/runtime/test_failures.py::test_classifies_expired_managed_credential_*, ::test_bare_unauthorized_log_string_is_not_classified, ::test_sanitized_evidence_redacts_tokens`; `tests/runtime/test_executor.py` | none |
| 1D Bounded recovery by cause | A | Finite default attempt budget; fresh credential per repair; recovery by cause table; restart cannot renew exhausted budget; direct execution cannot bypass durable history | `runtime/scheduler.py:RecoveryPolicy:38`, `_retry_blocked_by_cause:689`, `_total_attempt_exhausted:746`; `runtime/attempts.py:count_consumed_attempts:232`; `runtime/executor.py:_repair_within_total_account:850` | `tests/runtime/test_jobs_scheduler.py::test_scheduler_stops_mixed_fresh_and_repair_attempts_at_total_bound_and_survives_restart`; `tests/runtime/test_executor.py::test_direct_executor_launch_cannot_bypass_durable_attempt_history`; commits `05bd38b`, `7da6d51`, `02183f8` | none |
| 1E Preserve evidence before cleanup + reconcile restarts | A | Persist logs/context identity/failure record/recoverable changes before destructive cleanup; reconcile process existence/lease/attempt state on restart | `runtime/executor.py:_preserve_failure_evidence:1254`, `_fail_worker:1178`; `runtime/jobs.py:settle_interrupted_attempts:276`; `runtime/attempts.py:reconcile_attempt_records:198` | `tests/runtime/test_executor.py::test_executor_unexpected_worker_exit_fails_job_and_preserves_evidence_and_releases_lease`; `tests/runtime/test_attempts.py::test_store_tracks_admitted_running_ended_distinction`; commit `ead05c9` | none |

### Plan 2 — Complete, reproducible worker context

| Step | Status | Concrete criterion | Code reference | Test / run evidence | Gap owner |
| --- | --- | --- | --- | --- | --- |
| 2A Trace and unify input resolution | A | Both legacy planning and implementation prompt routes consume the same resolver/compiler output at job creation | `runtime/execution_contracts.py:compile_standard_execution_contract:179`; `runtime/context.py:LinkedContextResolver.build_context_packet:61`; `runtime/planning_inputs.py:freeze_planning_inputs:32`; `runtime/task_manager.py:create_execution_job:207` | `tests/runtime/test_context.py`; `tests/runtime/test_planning_input.py::test_planning_inputs_deduplicate_sources_and_do_not_import_linked_files:139`; commits `9283f49`, `50c1901` | R6 (freeze repository baseline in planning inputs) |
| 2B Freeze source bytes with provenance | A | Frozen context bundle with role/original-ref/digest/byte-count/required/optional/workspace-path/reason; full bytes materialized under `.open-tulid/context/`; dedup with provenance; admission blocked when required parent context unavailable | `runtime/execution_contracts.py:_freeze_linked_context:596`, `_context_workspace_file_path:733`; `runtime/workspaces.py:_write_frozen_context_files:252`; `runtime/task_manager.py` | `tests/runtime/test_workspaces.py::test_workspace_materializes_frozen_context_files_and_manifest:144`; `tests/runtime/test_context.py`; `tests/runtime/test_execution_contracts.py::test_workspace_writes_frozen_contract_files_and_rejects_repo_drift:290`; commits `551d0a6`, `5f2bd4c`, `1ff701b`, `990247d` | R6 (repo-baseline provenance beyond required files) |
| 2C Render one coherent prompt | A | Stable 5-section order; one authoritative task body; exact workspace paths named; repository facts; procedure + global commands; completion protocol | `runtime/prompts.py:compile_execution_prompt:125`, `_compile_implementation_prompt:433`, `_required_reading_text:514` | `tests/runtime/test_execution_contracts.py::test_prompt_lint_rejects_unresolved_and_missing_reading_paths:969`; commit `6557898` | none |
| 2D Make budget handling explicit | A | Named budget error for oversized required docs (no truncation); optional trimming first; lint flags duplicate authoritative content/unresolved paths/missing required sources/hash mismatches | `runtime/prompts.py:PromptBudgetError:56`, `_fit_total_budget:751`; `runtime/prompt_versions.py` | `tests/runtime/test_execution_contracts.py::test_full_task_exceeding_budget_fails_with_named_budget_error:807`; commit `ae8919f` | none |
| 2E Align preview, repair, and review | A | Preview calls same resolver/compiler, no scheduler mutation; saved-job reads historical bundle; normalizes only ephemeral fields; repair keeps required files; review blocks without immutable evidence | `runtime/prompts.py:normalize_ephemeral_completion_fields:198`, `find_review_evidence:140`; `runtime/task_manager.py`; `cli/main.py` | `tests/runtime/test_jobs_scheduler.py::test_preview_matches_scheduled_implementation_packet_from_identical_inputs:587`; `tests/test_operator_cli.py::test_prompts_render_compare_job_matches_scheduled_packet_without_mutating_scheduler:985`; commit `a2a589d` | none |

### Plan 3 — Consistent, executable tasks and complete breakdowns

| Step | Status | Concrete criterion | Code reference | Test / run evidence | Gap owner |
| --- | --- | --- | --- | --- | --- |
| 3A One parser and validation path | A | One pure body validator shared by derived-artifact acceptance, tracker load, repair preview, job preparation; title/description/required sections/duplicate headings/nonempty acceptance; forbidden command fields rejected with file/section diagnostics | `vault/task_schema.py:parse_task_body:73`, `validate_task_schema:145`, `_validate_acceptance:191`; consumed at `runtime/completion.py:_parse_derived_task_file:1782`, `runtime/task_manager.py:224` | `tests/test_task_schema.py::test_accepts_valid_body`,`test_duplicate_heading_reported`; `tests/runtime/test_completion.py::test_completion_rejects_invalid_derived_task_after_validation_passes:1230`; commit `9ed94db` | none (*) |

(*) 3A: the optional `body_schema: implementation/v1` DSL field was intentionally
not added; the schema association is expressed through task type
(`task_uses_global_contract`, `runtime/task_contracts.py:146`), which the plan's
conditional permits. This is a documented design decision, not a blocking gap.

| 3B Define semantic revision and source ownership | A | Semantic revision from normalized task requirements, dependency identities, selected required source-content identities; excludes board state/timestamps/audit links; revision separate from immutable packet hash | `runtime/attempts.py:task_semantic_revision:79`; `runtime/context.py:resolve_source_content_identities:344`; consumed at `runtime/completion.py:1157`, `runtime/scheduler.py:235` | `tests/runtime/test_attempts.py::test_semantic_revision_ignores_board_state_timestamps_and_audit_links:96`, ::test_semantic_revision_changes_when_required_source_content_changes:170; commits `e2a14d3`, `0c3124d` | R5 (legacy historical-attempt ambiguity) |
| 3C Strengthen planning inputs and instructions | A | Repository facts + unfinished-task inventory (IDs/states/dependencies) to planning; self-audit checklist; dedup; no recursive import of linked files | `runtime/planning_input.py:build_planning_inputs:40`, `_unfinished_tasks_in_scope:80`; `runtime/executor.py`; `templates/default_project/agents/task-breakdown.agent.md` | `tests/runtime/test_planning_input.py::test_planning_inputs_list_unfinished_tasks_with_ids_states_dependencies:97`; `tests/runtime/test_executor.py::test_render_execution_prompt_injects_planning_inputs_for_breakdown_transition:341`; commits `917f3ce`, `2e4ec76` | R6 (repo-baseline freeze; answer precedence diagnostics) |
| 3D Validate and publish batch atomically | A | Parse/validate every child before any promotion; unique local IDs, missing/self/cyclic deps, body schema, source links, artifact path consistency; IDs allocated only after validation | `runtime/completion.py:_derived_task_plan:1633`, `_derived_cycle_errors:1731`, `_allocate_numeric_task_ids:1771` | `tests/runtime/test_completion.py::test_new_implementation_batch_validates_schema_before_any_promotion:1391`, ::test_completion_rejects_batch_with_one_invalid_child_leaves_zero_partial:1494; commit `ab58782` | R6 (concurrent batch publication + interruption recovery) |
| 3E Migrate templates and active task definitions | P | Authoring templates/parser/validator/CLI/docs updated together; migration preview on tracker copy; preserve old IDs/dependency meaning/non-goals/behavioral criteria | Templates under `templates/default_project/agents/`; `adapters/obsidian.py`; `vault/task_schema.py`; preview `docs/migration-preview-plan-3e.md` | `tests/adapters/test_obsidian_adapter.py::test_machine_accepts_selector_rejected:580`; commit `2494ba9` | R6 (executable migration/historical-loading tooling); R11 (migrate live product task definitions on isolated tracker) |

### Plan 4 — Reproducible verification of the actual product

| Step | Status | Concrete criterion | Code reference | Test / run evidence | Gap owner |
| --- | --- | --- | --- | --- | --- |
| 4A Specify verifier request/report | A | VerificationRequest carries candidate/baseline digest, command-policy digest, image identity, env identity, ordered commands; report carries per-command results + granular classification; legacy report reader retained | `runtime/verifier.py:VerificationRequest:247`, `VerificationReport:152`, `_granular_classification:985`; `runtime/verifier.py:23` (v1 reader) | `tests/runtime/test_verifier.py::test_request_freezes_ordered_commands_ignoring_compile_reshuffle:96`, ::test_report_carries_request_policy_candidate_and_environment_identity:144; commit `19d4aaa` | none |
| 4B Freeze and execute in declared environment | A | Verification copy/container from immutable candidate using worker image identity; writable copy; process-tree termination; lockfile identity; host-independent | `runtime/verification_runtime.py:VerificationEnvironment:61`, `prepare_verification_copy:89`, `ContainerCommandExecutor:272`; `runtime/verifier.py:_run_contract_checks:889` | `tests/runtime/test_verification_runtime.py`; `tests/e2e/test_standard_contract_e2e.py::test_verification_executes_in_the_declared_project_container:176` (Docker-gated); commit `6c3186c` | R7 (whole-project components/suites in the real image) |
| 4C Preserve global command semantics | A | No alphabetical sort of checks; order from contract.yaml frozen into policy identity; same global list rendered in implementation and review prompts; policy frozen at admission | `runtime/execution_contracts.py:compile_standard_execution_contract:179` (order-preserving), `_validate_global_commands:269`; `runtime/verifier.py:verification_request_from_execution_contract:303` | `tests/runtime/test_verifier.py::test_request_freezes_ordered_commands_ignoring_compile_reshuffle:96`; `tests/runtime/test_executor.py::test_executor_command_policy_hash_preserves_declared_order:100`; commit `4b8dac2` | none |
| 4D Cover all promised project components | P | Project-owned verification entry point / ordered commands covering backend, Node/Python contracts, frontend build/tests, deterministic integrated behavior; discovery assertions, not name checks; delete/lock removal fails | `examples/wealthy-scholar-verification/contract.yaml`, `examples/wealthy-scholar-verification/tools/verify_project.py`; `runtime/verification_runtime.py`; `runtime/candidate.py:iter_deliverable_files` | `tests/runtime/test_project_components.py::test_all_components_execute_for_implementation_and_review:79`, ::test_required_component_deletion_fails:107; commits `2148175`, `6501ebb` | R7 (establish every required component/suite in the isolated application; bootstrap+final coverage) |
| 4E Return useful evidence and guard coverage regressions | A | Command-specific output to worker; distinguish assertion failure vs env blocker; coverage-change guard; recompute deliverable digest after checks; promote immutable candidate | `runtime/verifier.py:CoverageChange:127`, `_coverage_changes:802`, `_workspace_deliverable_manifest:946`; `runtime/repairs.py:build_repair_packet` | `tests/runtime/test_verifier.py::test_coverage_guard_records_test_and_build_discovery_changes:390`, ::test_coverage_guard_surfaces_a_deleted_suite_for_review:421; `tests/runtime/test_repairs.py`; commit `8366d6b` | none |

### Plan 5 — Deliver exactly the verified changes

| Step | Status | Concrete criterion | Code reference | Test / run evidence | Gap owner |
| --- | --- | --- | --- | --- | --- |
| 5A Define deliverable snapshot surface | P | Audit exclusions; retain versioned source under `build`/`output`/`dist`; handle binary/empty/executable/hidden; reject unsupported entries/symlinks with precise diagnostic; track source under cache-like names (e.g. `node_modules`) retained | `runtime/repository_facts.py:EXCLUDED_DIRECTORY_NAMES:21`, `_repository_files:245`; `runtime/candidate.py:CANDIDATE_EXCLUDED_DIRECTORY_NAMES:46`; `runtime/workspaces.py:_copy_repo:147` | `tests/runtime/test_repository_facts.py::test_repository_snapshot_captures_deterministic_facts_and_baseline`; `tests/runtime/test_candidate.py::test_snapshot_rejects_symlinks_instead_of_sealing_mutable_external_bytes`, ::test_capture_candidate_excludes_internal_and_ephemeral_trees; commits `8547f28`, `1fcf077` | R2 (complete Git tracked/non-ignored rule — name-only walk still vulnerable for tracked source under cache names); R3 (path-type transitions) |
| 5B Capture stable candidate at submission | A | Candidate captured in Tulid-owned storage outside worker mount; stable or explicit reject; sealed; authoritative delta with added/edited/deleted/modes, not relying on `changed_files`; genuine no-change candidate | `runtime/candidate.py:capture_candidate:123`, `_compute_delta:300`, `_discrepancy_note:327`; `runtime/completion.py:_capture_completion_candidate:774` | `tests/runtime/test_candidate.py::test_capture_candidate_computes_authoritative_delta`, ::test_capture_candidate_supports_genuine_no_change, ::test_executable_change_is_verified_and_delivered; commits `d575de5`, `5bdddee`, `0b256e3`, `257aa8f` | none |
| 5C Acquire repository lane and check target | A | Canonical repository identity (not project name); one serial lane per repo; target baseline/branch/manifest checked; stale/conflict preserves candidate; no clean of user checkout | `runtime/repository_facts.py:repository_identity:217`; `runtime/scheduler.py:_serial_repo_lane_focus:508`; `runtime/completion.py:_check_integration_target:1172` | `tests/runtime/test_jobs_scheduler.py::test_scheduler_blocks_concurrent_integration_across_projects_that_share_a_repository`, ::test_scheduler_keeps_repo_lane_unavailable_on_unresolved_acceptance; `tests/runtime/test_completion.py::test_completion_rejects_when_target_repository_changed_since_baseline`; commits `35cba8c`, `7d09d09` | none |
| 5D Promote through durable acceptance transaction | A | Prepare journal before effects; apply from sealed candidate incl. deletions; single Git commit with recorded parent/transaction identity; validate integrated manifest vs verified candidate before finalizing artifacts/task/board acceptance; repo lane released only after acceptance | `runtime/transactions.py:FileTransactionRuntime.apply:45`; `runtime/completion.py:_apply_acceptance:882`, `_commit_plan:1476`, `_commit_repo_changes:1510`, `_validate_integrated_source:1377` | `tests/runtime/test_transactions.py::TestFileTransactionRuntime`; `tests/runtime/test_completion.py::test_completion_promotes_deletion_from_sealed_candidate`, ::test_compensation_blocks_when_promoted_target_changed; commits `7614cf2`, `82504d8`, `efe7e5a` | none |
| 5E Recover without losing user work | P | Roll forward only provably safe operations; compensate only when current bytes match own after-image; stop on crash-after-commit by locating/verifying the commit (no duplicate); repeatable single result or explicit unresolved conflict; intervening user change blocks | `runtime/completion.py:recover_completion_transactions:1879`, `_recovery_effect_applied:2006`, `_recovery_has_conflict:2064`, `_transaction_commit_exists:2129`, `_settle_recovered_acceptance:1969` | `tests/runtime/test_completion.py::test_recovery_preserves_intervening_writes_and_deletions`, ::test_recovery_restores_job_acceptance_across_commit_crash, ::test_recover_completion_transactions_blocks_on_intervening_delete_change; commits `06af3ec`, `e91f580`, `fab28fa`, `a2575d3` | R4 (bind recovery to exact Git tree/original accepted identity — `_transaction_commit_exists` recognizes branch tip/parent/subject, not tree identity) |
| 5F Feed reliable evidence to review and scheduling | A | Stored actual add/edit/delete paths, source identities, check evidence, commit/transaction IDs on acceptance; dependent admitted against accepted repo identity, not board move | `runtime/completion.py:submit:577-644` (acceptance_metadata); `runtime/scheduler.py:_dependency_accepted_repo_identity:892`; `runtime/acceptance.py:accepted_task_evidence:14` | `tests/runtime/test_jobs_scheduler.py::test_scheduler_admits_dependent_against_accepted_repository_identity`, ::test_scheduler_rejects_dependent_admitted_on_board_move_without_acceptance; commits `6902fb6`, `a537882` | none |

### Plan 6 — Review, truthful completion, and proof of a finished product

| Step | Status | Concrete criterion | Code reference | Test / run evidence | Gap owner |
| --- | --- | --- | --- | --- | --- |
| 6A Define successful task completion in the domain | P | Shared predicate for successful implementation completion (verified candidate + committed delivery + required review evidence + consistent final state); declared terminal outcomes; no name-based inference; undeclared terminals readable but not implicit success | `domain/completion.py:SuccessfulCompletionCriteria:20`, `terminal_outcome_of:69`, `dependency_outcome:99`; `domain/schema.py:StateDefinition.terminal_outcome:230`; `workflow/compiler.py:_validate_terminal_semantics:335` | `tests/runtime/test_jobs_scheduler.py::test_scheduler_blocks_dependent_on_declared_failure_terminal`, ::test_scheduler_uses_renamed_states_and_custom_task_types_for_success`; `tests/workflow/test_compiler.py` | R1 (the two uncommitted edits in §4 change undeclared outcome from implicit success to `ambiguous` — unverified and owned by R1) |
| 6B Build requirement-driven review packet | A | Review packet consumes frozen task/spec/answers + actual change set/source identity + check reports + bounded repair history; review maps each requirement to code/test and reports defects/fixes/blockers | `runtime/prompts.py:find_review_evidence:140`, `_compile_review_prompt:598`, `_review_evidence_text:955`; `runtime/task_manager.py:326` | `tests/runtime/test_completion.py::test_review_submission_requires_and_persists_compact_review_result`, ::test_review_rejects_malformed_blockers; commit `bda7c66`, `29b6849` | none |
| 6C Align manual operations, state, and recovery | P | Manual implementation success requires same committed acceptance evidence; missing evidence rejected with state intact; manual business/cancel transitions remain valid; dependent admission blocked when evidence stores unavailable | `runtime/task_manager.py:_manual_implementation_acceptance_error:501`, `request_transition:132`; `runtime/scheduler.py:_dependency_error:806`; `runtime/acceptance.py:accepted_task_evidence:14` | `tests/runtime/test_task_manager.py::test_request_transition_rejects_manual_implementation_without_acceptance`, ::test_request_transition_keeps_manual_business_transition_without_acceptance`; `tests/runtime/test_acceptance.py::test_code_acceptance_requires_delivered_commit_in_current_history`; commits `cf9a49b`, `930f11b`, `803d588`, `e1b3152` | R1 (`_dependency_error` gates recorded acceptance only when repository root + job store + project ID are all supplied — missing-store route) |
| 6D Establish the project's product acceptance inventory | P | Inventory maps canonical spec journeys to tasks/verification; confirm expectations against canonical source; fixture-based integrated checks traverse real boundaries; assertions detect intentionally broken behavior | `examples/wealthy-scholar-verification/product-acceptance-inventory.md` (commit `743f74f`) | `tests/test_prove_workers_harness.py` (harness only); no per-journey Wealthy Scholar assertions executed against canonical source yet | R7 (whole-project env + fixture journeys); R11 (product acceptance journeys) |
| 6E Run deterministic fault and scripted chains | P | Multi-round clarification, generated batch, dependent execution, failed completion/repair, review, exact repository delivery; fault matrix (auth expiry, worker death, stop/restart, malformed artifacts, missing context, stale target, omitted paths, deletion, crash during acceptance) ends bounded; tracker/repo consistent; single/distinct worker; renamed states/types | `tests/e2e/test_scripted_fault_chains.py` (+ `tests/e2e/fixtures/scripted-worker-runtime/scripted_worker.py`) | `tests/e2e/test_scripted_fault_chains.py::test_chain_multiround_batch_dependency_review_exact_delivery_distinct_workers`, ::test_chain_batch_dependency_review_single_worker_all_steps`, ::test_fault_worker_death_during_submission_recovers`, ::test_fault_malformed_planning_artifact_blocks`, ::test_fault_missing_context_blocks`; commit `b41649e` | R9 (map each fault to existing test + extend; authentication expiry with fake time, stop/restart after exhausted attempts, same-message/wrong-tree recovery not yet covered) |
| 6F Prove actual configured workers and complete product | EB | Actual worker chain on isolated checkout/tracker with configured worker assignments; ≥3 dependent implementations through planning/review/delivery; sustained >1 h attempt; controlled stop/restart; 3 repeat chains; full product acceptance from clean checkout | harness `tests/test_prove_workers_harness.py`; `tests/e2e/fixtures/mock-agent/mock_agent.py`; durable isolation `.reliability-proof/20260911/isolation/` (commits `740464b`, `fd09491`, `19b2467`, `61708a6`, `19d0cb1`, `94e21ee`) | No real worker chain launched: `http://127.0.0.1:8080/health` refused connections on latest probe; run-ledger `completed: 0`; sustained/restart/repeat criteria unproven | R10 (restore configured `peon` service — external prerequisite; then actual worker proof); R11 (full product) |

## 7. Outstanding owners (every remaining item has an owner)

| R-package | Closes |
| --- | --- |
| R1 | 6A (uncommitted terminal-outcome edits, §4), 6C (missing-store dependency gate) |
| R2 | 5A (complete Git tracked/non-ignored source selection through transport) |
| R3 | 5A/5E (path/type transitions, link races beyond name-only rules) |
| R4 | 5E (exact recovery/Git-tree identity) |
| R5 | 1A, 3B (historical/legacy attempt accounting; semantic-revision edge cases) |
| R6 | 2A/2B/2C (frozen repository baseline in planning), 3C (answer precedence), 3D (concurrent batch publication), 3E (executable migration) |
| R7 | 4B/4D/6D, 6E (whole-project component setup, suites, bootstrap/final coverage) |
| R8 | harness truthfulness (runs against real runtime records) |
| R9 | 6E (integrated deterministic fault chains on final revision) |
| R10 | 6F (configured-worker representative proof, external `peon` blocker) |
| R11 | 6D/6F (original product completion and integrated acceptance) |
| R12 | reconciliation/final report |

## 8. Honesty notes and limitations

- **No final-tree test pass is claimed.** Both retained logs predate the last
  four commits and the two uncommitted R1 edits. `accepted` (§6) means inspected
  code + tests exist and exercise the behavior at the named seams; it is not a
  green final-tree run. The final-tree integrated gate is R9's scope.
- **Examples, written plans, board states, and process exits are not proof.** No
  entry in this matrix treats `product-acceptance-inventory.md`, plan prose,
  tracker board positions, or a CLI exit code as product/worker acceptance.
- **The private-Mongo 226-test log is lost** (host restart) and is not cited as
  retained evidence; reproduction is an R7 acceptance case.
- **6F is externally blocked** by the unavailable configured `peon` service, not
  by any product/implementation failure.
- **The two uncommitted R1 edits are unverified**; R0 records rather than
  validates them.
- Status assignments where a step is marked `accepted` with a non-empty owner
  reflect that the modern path is evidenced while the named residual (audit
  target or confirmed gap) still has an owner; `partial` is used where the
  original criterion itself is not yet fully evidenced on the current tree.

## 9. Commands and result references

| Command / artifact | Location |
| --- | --- |
| Full suite (997 passed, 126.33 s) | `.reliability-proof/20260911/full-suite.log` (Git-ignored; predates final tree) |
| Deterministic suite (1 failed, 968 passed, 47.23 s) | `.reliability-proof/20260911/deterministic-suite.log` (Git-ignored; separate run) |
| Image build | `.reliability-proof/20260911/image-build.log` |
| Model proxy | `.reliability-proof/20260911/model-proxy.log` |
| Task-7 preview | `.reliability-proof/20260911/task7-preview.log` |
| Isolated experiment | `.reliability-proof/20260911/isolation/` (project, tracker, tracker-history) |
| Run ledger (completed: 0) | `.reliability-proof/20260911/run-ledger.{json,md}` |
| Migration preview | `docs/migration-preview-plan-3e.md` |
| Product acceptance inventory | `examples/wealthy-scholar-verification/product-acceptance-inventory.md` |
| Default global policy example | `examples/wealthy-scholar-verification/contract.yaml`, `tools/verify_project.py`, `tools/with_test_database.py` |

## 10. Global verification status

R0 is an accounting package and performs no new product/worker run. The full
Tulid gate for the final working tree is an R9/global-gate responsibility and is
**not** inferred from the earlier runs. See §5.
