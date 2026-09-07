# Tulid reliability and polish plan

Prepared 2026-09-07 from the current working tree, Wealthy Scholar's Obsidian project, and saved execution records. This is a plan, not an implementation change.

The target is the existing workflow: product idea → clarification/specification/breakdown → implementation and review → verified, integrated product. Every task is assigned to configured `<workers>`. Users choose the worker tools, providers, and models, and may use the same worker for several responsibilities or different workers for each. Tulid must not assume fixed worker tiers or a tool/model for any responsibility. Keep the project scope, global verification commands, and freedom to edit the files a task needs. Do not restore generated per-task validation contracts or file allowlists.

## Evidence and limits

The concrete project inspected is `/home/rawsteel/repo/obsidian/wealthy-scholar`, with application source in `/home/rawsteel/repo/wealthy-scholar`. No separately named Wealthy Researcher project was found in the vault file inventory. Existing uncommitted Tulid changes were preserved. No application source, tracker records, running workers, or model services were changed.

| Finding | Evidence | Implication |
| --- | --- | --- |
| Model credentials expire before the allowed job duration. | `runtime/model_proxy.py` gives both session stores a 3,600-second default; CLI construction uses that default. Local runtime configuration allows 7,200-second workers. No session renewal path was found. | A capable worker can lose model access halfway through its permitted task. |
| The latest task #8 failures fit that expiry boundary. | Jobs `01M1XNK0NNMY02JVPFM25HSAKG` and `01M1XT19QEK63SB4HK6ZJX0WWN` ran about 62m41s and 61m25s, respectively. Both end in `Unauthorized: unauthorized`; job metadata records only return code 1. | Credential expiry is the leading diagnosis. It is not proven for these historical runs because authentication rejection reasons were not logged. `doom_loop: deny` configuration alone does not prove a doom-loop denial. |
| Detailed planning context is lost at implementation. | `compile_standard_execution_contract` freezes `context_excerpts=()`. The saved task #8 prompt explicitly says no excerpts were selected, while the task depends on enums and rules in the implementation specification. | The assigned implementation worker must infer decisions already made during planning. The full task is repeated in the contract section, so the truncated Mission is not evidence that its entire task body was lost. The absent specification is the more serious gap. |
| Task authoring and task validation disagree. | The breakdown template permits free-form Markdown without mandatory sections; `vault/task_schema.py` requires description/Why/What/How/Acceptance. New authoring forbids `accepts:` lists, but validation still supports them and current tasks contain them. | Validity depends on which part of Tulid reads the same task. Template updates alone do not migrate existing tasks or frozen jobs. |
| Trusted checks use a different environment from the worker. | `_run_contract_checks` invokes host `subprocess.run` in the worker workspace. Worker images omit Python/uv in the base image, and Wealthy Scholar's project Dockerfile adds no tools. | Host/container tools, dependencies, and permissions can disagree. The exact installed project image still needs auditing before a live run. |
| The project's current check policy cannot establish completion of all planned work. | Wealthy Scholar's `contract.yaml` runs only `npm test --prefix backend`. Task #8 includes Python parity/lockfile work; task #20 includes frontend build/browser behavior. | Passing the configured command does not independently establish those requirements. A name such as `project_tests` or `vertical_slice` does not add missing coverage. |
| Verified workspace contents and promoted output can differ. | Workspaces exclude `.git`; `_git_changed_files` then returns no diff. Global verification reports empty change lists. `_changed_file_plan` copies only worker-submitted paths that currently exist. The verifier rejects submitted deleted paths. | Omitted files can be tested but not delivered; deletions cannot be delivered through this path. This also weakens self-review's change evidence. |
| Retry limits and failure evidence need consolidation. | The executor has generic nonzero-exit handling. Scheduler failure counting can be limited to the current runtime session; completion repair has a separate budget. | Restarting the runtime can reset the effective failure history. Repeating a job does not necessarily address its cause. |
| Tracker Done is not sufficient historical proof. | On September 4, task #7 received `ImplementTask` and `SelfReview` transition events without job/submission evidence after earlier rejections. | Treat that historical result as needing verification, not as proof of an autonomous implementation/review success. This does not establish that its code is wrong. |

Validation performed: `.venv/bin/python -m pytest -q --ignore=tests/e2e` → **661 passed**, with two multiprocessing warnings, after allowing local loopback access for test HTTP traffic. The first sandboxed run had four failures and four skips involving restricted local networking; the unrestricted rerun passed. Docker end-to-end and real-model tests were not run. These passing tests establish a useful baseline, not the full product guarantee.

Source pointers are relative to `src/open_tulid/` unless otherwise stated. Historical evidence lives in the vault's `events/2026-09-04.log`, `events/2026-09-07.log`, and `~/.tulid/workspaces/<job-id>/.open-tulid/`.

## Working definition of reliable

An accepted task means its required behavior was checked, the exact verified changes reached the project repository, the task and board agree, and dependent tasks see that accepted result. A failed task either repairs with actionable evidence within a durable bound or stops with a specific blocker and preserved work. It never silently loses necessary context or spins indefinitely.

A finished product additionally passes the existing product specification's integrated user journeys in a clean environment. Individual green tasks are necessary, but are insufficient evidence of product completion. Human answers remain necessary for genuinely unresolved product decisions; routine execution should not require manual board movement, rewritten completion payloads, or repeated restarts.

## Six detailed implementation plans

The original eight work packages are consolidated into the six priorities below. Baseline and schema consistency belong to task quality; review and whole-chain proof belong to product completion. Each linked plan contains implementation steps, affected code, regression cases, migration, dependencies, and completion evidence.

| Priority | Individual plan | Original work packages |
| --- | --- | --- |
| 1 | [Reliable worker execution and recovery](reliability-01-execution-plan.md) | Credential lifetime, failure classification, durable retry limits, preserved work; runtime baseline from package 1. |
| 2 | [Complete, reproducible worker context](reliability-02-context-plan.md) | Package 3: task/specification/answer handoff, frozen readable references, prompt budgets, preview and review consistency. |
| 3 | [Consistent, executable tasks and complete breakdowns](reliability-03-task-quality-plan.md) | Packages 1 and 4: shared task shape, planning quality, dependency validation, source revisions, atomic breakdown, migration. |
| 4 | [Reproducible verification of the actual product](reliability-04-verification-plan.md) | Package 5: project toolchain, ordered global checks, complete test discovery, exact-candidate evidence. |
| 5 | [Deliver exactly the verified changes](reliability-05-delivery-plan.md) | Package 6: complete change sets, deletions, target conflicts, recoverable integration, dependency release. |
| 6 | [Review, truthful completion, and proof of a finished product](reliability-06-product-completion-plan.md) | Packages 7 and 8: workflow success semantics, requirement-driven review, real-worker chains, integrated product acceptance. |

## Shared design rules

- `<workers>` are configured executors assigned to tasks/transitions. Planning, implementation, and review describe the work, not permanent worker categories. Provider-specific behavior stays in adapters; scheduling and completion consume generic outcomes.
- Task text explains behavior and context. The project global contract supplies verification commands. No per-task command selection or predicted writable-path acceptance is introduced.
- A semantic task revision identifies the required work; an attempt identifies a worker execution; a candidate identifies proposed source bytes; a verification report binds checks to that candidate; a committed acceptance record binds delivery and workflow success. These are internal extensions of existing records, not a new worker-authored contract.
- Required context cannot silently disappear. Failed work cannot disappear before recovery evidence is retained. Accepted output cannot differ from verified output unnoticed.
- Existing specification and product acceptance expectations define scope. This effort adds no product features, mandatory worker hierarchy, model substitution, or extra planning/review stage.

## Implementation order and dependencies

The numbers identify priorities, not a requirement to finish each whole plan before starting the next.

1. Capture a reproducible runtime/project baseline from plan 1 and agree semantic task revision/reference ownership from plan 3. Fix credential lifetime and failure evidence first.
2. Reconcile task shape and batch validation in plan 3, then integrate the frozen context path in plan 2. Retry semantics in plan 1 consume the same revision identity.
3. Implement candidate capture and manifest rules from plan 5, steps 5A–5B. Plan 4 consumes these to verify a stable candidate in the project environment.
4. Finish plan 5's integration/recovery using plan 4's exact-candidate reports. This staged sequence avoids a circular dependency between verification and delivery.
5. Integrate review and success semantics from plan 6. Prepare its deterministic fixtures and product acceptance inventory earlier, but run its release gate only after plans 1–5 are integrated.

Each implementation PR should cover one bounded step with its own regression evidence and preserve existing work. Use the existing CLI, logs, workflow, project images, contracts, and transaction system. Do not repeatedly spend hour-long model runs diagnosing defects that fake-clock or scripted tests can expose.

Migrate active project definitions on an isolated tracker/repository copy first. Historical artifacts and frozen jobs retain their original meanings. Record which guarantees old evidence cannot establish rather than upgrading it retroactively.

The first actual-worker experiment should test corrected credential lifetime and complete task #8 context on an isolated copy using the user's configured workers. Its result informs task decomposition; the historical infrastructure failures alone are not evidence that a different tool/model or smaller tasks are necessary.
