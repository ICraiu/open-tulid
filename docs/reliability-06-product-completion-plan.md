# Plan 6 — Review, truthful completion, and proof of a finished product

Status: implementation and validation plan; no live worker chain or product acceptance run has been performed by writing this document.

Parent: [reliability and polish plan](reliability-polish-plan.md). This includes the original review/Done work and complete-chain validation.

## Outcome and boundaries

Tulid distinguishes a worker exiting, a task being verified and integrated, a successful review, and the agreed product actually working. It advances dependencies only from the intended successful outcome, and it proves completion through the original product's integrated behavior.

All responsibilities belong to configured `<workers>`. A user may assign one worker to every transition or choose different tools/models for planning, implementation, and review. Neither review rigor nor success semantics may depend on a particular provider or an assumed stronger/weaker worker tier.

Use existing planning, review, completion, workflow, CLI, and event mechanisms. Do not add a mandatory external reviewer, a new dashboard, model switching, or product requirements beyond the existing specification.

## Starting evidence and code

- `runtime/prompts.py`: `find_review_evidence`, `_compile_review_prompt`, and `is_review_transition` determine review inputs/behavior.
- `runtime/task_manager.py`: `request_transition` is a manual path whose guarantees must be aligned with completion acceptance.
- `runtime/scheduler.py`: dependency handling treats absence of outgoing transitions as finished, which is insufficient to distinguish success from a failed/cancelled terminal state.
- `runtime/completion.py`, `runtime/transactions.py`: trusted acceptance and durable delivery evidence.
- `domain/schema.py`, `workflow/compiler.py`, `workflow/runtime.py`, `src/workflow_engine/`: configurable workflow semantics.
- `templates/default_project/agents/self-review.agent.md`, authoring templates, and workflow defaults: instructions and completion policy.
- Existing deterministic/scripted tests in `tests/runtime/` and `tests/e2e/` provide a foundation but do not prove the quality of actual worker output.

Wealthy Scholar task #7 has historical transition events without matching job/submission evidence at that transition point. Its source may be correct, but its board state is not sufficient proof of an autonomous verified result. Revalidate the baseline instead of rewriting history.

## Decisions

1. Verification proves global commands ran successfully on exact source bytes; review additionally checks task behavior and test adequacy. Neither implies that untested product journeys work.
2. Reuse one configured review transition unless the user's workflow already specifies otherwise. The reviewing worker receives the original requirements and authoritative implementation evidence. It may fix defects within scope, which creates a new candidate requiring verification and delivery.
3. Success/failure/cancellation semantics belong in the workflow. Do not infer them from the spelling of state IDs, task type names, worker IDs, or model/tool choices.
4. Use current DSL capabilities where sufficient. Where missing, add the smallest explicit state outcome declaration, proposed `terminal_outcome: success | failure | cancelled`, with compiler validation and an explicit default-workflow migration. If review selection currently relies on transition-name heuristics, replace it with an explicit transition semantic declaration rather than provider checks. Document the exact narrow schema change before implementing it.
5. A manual movement cannot masquerade as verified implementation success. Manual business/clarification transitions remain available as defined by the workflow. Any existing intentional override remains visibly distinct and does not fabricate verification evidence.

## Implementation sequence

### 6A. Define successful task completion in the domain

Audit automatic completion and manual transition entry points. Define one shared predicate for successful implementation completion: required global checks accepted for the candidate, delivery transaction committed, required review evidence present where configured, and final task/board state consistent.

Use declared terminal outcomes for dependency success. Reject or diagnose workflows where a supposedly terminal state has contradictory outgoing transitions. Legacy workflows with ambiguous terminal semantics need an explicit migration diagnostic; preserve historical readability and do not reinterpret a failed terminal state as success.

Keep artifact-only planning transitions valid under their own requirements. Do not require a code diff or application test report from a question-generation task. Tests must include renamed states, custom task types, and arbitrary configured workers to demonstrate that semantics are not hardcoded.

### 6B. Build a requirement-driven review packet

Consume plan 2's frozen task/specification/answers, plan 5's actual change set and integrated source identity, and plan 4's check reports. Include bounded repair history with precise prior failures. No accepted implementation evidence means review cannot pretend to inspect a verified implementation.

In existing review instructions, require the worker to compare each task requirement with actual code and tests, examine relevant integration seams, and identify missing behavior, ineffective tests, regressions, scope drift, and unfinished user-facing states. Inspect beyond changed files when a concrete requirement or failure points there.

Retain a compact requirement-to-evidence review result in existing completion/artifact records. It names the behavior, relevant source/test evidence, defects/fixes, and remaining blockers. Do not turn this into per-task command selection or accept generic assurances as equivalent to evidence.

A no-defect review may submit no code change. A corrective patch must stay within the assigned task, pass the same global policy, and be promoted as a new verified candidate. Missing product decisions become blockers for the existing clarification/planning path, not improvised redesign.

### 6C. Align manual operations, state, and recovery

Route implementation completion through the same acceptance checks whether invoked by runtime or an operator command. For a manual request lacking required evidence, explain what is missing and leave code/task/board state intact. Retain manual transitions that intentionally carry no implementation-success guarantee.

Expose completed-with-evidence, failed, interrupted, awaiting-input, and recovery-needed accurately through existing status/log commands. These are outcome/reporting distinctions; do not force every workflow to adopt literal state names. A board/frontend mismatch is a diagnostic/reconciliation problem, not permission to infer successful implementation.

Use plan 5's committed acceptance record for dependent admission. Pending or failed delivery/review cannot release the next task simply because a card was moved. Recovery must restore the original acceptance identity rather than producing another success event with no evidence.

### 6D. Establish the project's existing product acceptance inventory

From the canonical specification and answers, list the agreed product journeys, required failure cases, and existing usability expectations. Map them to the tasks/verification work from plan 3. This inventory is evidence of current scope, not a source of new features.

For Wealthy Scholar, include its specified grouped approval, run progress/reload, successful results, valid no-result outcome, minimum-data failure, provider/integrity failure, timeout without partial results, evidence navigation, notifications/feedback, legacy behavior preservation, keyboard use, and responsive layout. Confirm exact expectations against canonical source material before writing tests.

Implement fixture-based integrated checks that traverse the real boundaries: UI/API, services, persistence, stage execution, and cross-language contracts as applicable. Keep unit tests but do not replace these journeys with tests of mocked orchestration alone. Check that assertions detect intentionally broken behavior.

Revalidate the existing task #7 repository result in the new environment before using it as the baseline. Record findings and required fixes as original-scope work; preserve historical tracker evidence.

### 6E. Run deterministic fault and scripted chains

Extend the existing scripted end-to-end setup with multi-round clarification, a generated task batch, dependent execution, failed completion/repair, review, and exact repository delivery. Inject the known failures rather than relying on random model behavior.

Cover authentication expiry with fake time, worker death during submission, stop/restart with exhausted retry history, malformed planning artifacts, missing context, stale target, omitted changed paths, deletion, and crash during acceptance. Every scenario must end in a bounded accepted or explicitly blocked/failed outcome, with consistent tracker and repository state.

Run fixtures with a single worker assigned to all steps and with distinct worker implementations assigned to different steps. Also rename states/task types. This tests that the workflow determines responsibility and semantics, rather than implicit fixed worker roles.

### 6F. Prove actual configured workers and the complete product

Use an isolated repository checkout and tracker copy with the user's actual chosen worker assignments and existing model services. Record sanitized configuration, runtime/source identity, image identity, frozen policy/context, and initial source state. Ensure this experiment cannot write into the live project by accidentally retaining its configured paths.

Run at least three dependent implementation tasks from actual planning/breakdown through review and delivery. Include cross-language work and a specified user-facing integration requirement. Conduct a sustained coding attempt lasting beyond one hour and a controlled stop/restart. Fake-time tests prove the expiry boundary; the sustained run checks actual transport/tool behavior.

Repeat representative chains from their declared baselines three times. Record all attempts, retries, failures, verification/promotion evidence, duration, and manual interventions. Do not hide a failed run by resetting the tracker and reporting only the successful replacement. Keep real upstream/provider smoke checks distinct from deterministic fixture-based product acceptance.

Finally execute the full remaining Wealthy Scholar plan and run the agreed product acceptance inventory from a clean checkout of its delivered result. Demonstrate the usable product and its expected failure states. A three-task chain proves infrastructure continuity; it does not complete the full product by itself.

## Regression and acceptance matrix

| Case | Required result |
| --- | --- |
| Unrelated tests pass but a task requirement is deliberately omitted | Review identifies the concrete gap and prevents verified completion. |
| Review finds no defect | No forced cosmetic diff; global verification and valid review evidence still required. |
| Review changes implementation | New candidate is independently verified and delivered before Done. |
| Failed/cancelled terminal dependency | Dependent task remains ineligible. |
| Renamed/custom successful state and arbitrary worker assignment | Correct success/dependency behavior without name-based assumptions. |
| Manual implementation transition lacks verification/delivery evidence | Request is rejected or distinctly handled under an explicitly existing override policy; never recorded as ordinary verified success. |
| Planning artifact transition has no code diff | Works under its own configured requirements. |
| Card/state mismatch or unfinished acceptance recovery | Honest diagnostic; no premature dependent work. |
| Product integration or required UI state intentionally broken | Corresponding product acceptance check fails. |

Extend `tests/runtime/test_task_manager.py`, `test_jobs_scheduler.py`, `test_completion.py`, prompt/review tests, workflow compiler/runtime tests, operator CLI tests, and the existing scripted end-to-end suites.

## Release gates and evidence

This plan integrates plans 1–5. Domain/review tests and the product acceptance inventory can be prepared earlier; actual completion claims wait for the shared guarantees.

- All deterministic regressions and required scripted Docker checks pass; skipped checks are listed separately and do not count as passes.
- Three consecutive representative actual-worker chains complete within their declared retry budgets with no manual task reformatting, injected missing context, payload rewriting, state repair, or lost changes.
- The sustained attempt has no premature credential expiry; controlled restart preserves the effective budget and recoverable work.
- The delivered repository independently passes global verification and matches accepted source evidence.
- The full existing product plan and integrated product acceptance inventory pass before the product is called finished.

Deliver a run ledger, linked candidate/report/commit evidence, representative review records, fault-test results, and a product walkthrough tied to existing acceptance expectations. Separate observed success from untested general claims: these gates establish confidence for the tested workflow and configured workers, not a guarantee for every future product or worker assignment.
