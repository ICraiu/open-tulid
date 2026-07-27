# Improving Qwen Implementation Quality in Tulid

## Purpose

Tulid uses a strong model to decide what should be built and Qwen 3.6 27B to build it. The goal of this plan is to make that division of responsibility work better:

- The strong model makes the product, architecture, interface, sequencing, and testing decisions.
- Qwen receives a small, precise implementation assignment.
- Qwen runs the relevant tests while it works.
- Tulid independently reruns the tests before accepting the result.
- Existing end-to-end behavior must remain working after every task.
- Dedicated end-to-end tasks add coverage when a new user-visible flow is introduced.

This plan deliberately avoids a strong-model code review after every Qwen run. It also avoids building a separate prompt laboratory, test service, or task database.

## What the repository does today

The foundations are already present:

- Qwen runs through OpenCode using the local model configured in `~/.tulid/config.yaml`.
- Tulid creates an isolated workspace and a prompt packet.
- The strong model produces implementation specifications, Panalyzer proposals, and derived tasks.
- Qwen has an implementation pass followed by a self-review pass.
- Tulid can run trusted validation commands in a copied workspace before promoting changes.

The main gaps are:

- The Qwen prompt is long and repetitive. Scope rules and completion instructions appear more than once.
- Full parent and linked planning documents can compete with the current task for Qwen's attention.
- Task-generation prompts optimize for document size, module quotas, and diagrams instead of implementation clarity.
- The detailed validation commands written inside a task are only prose. Tulid does not execute them.
- STT-clipboard currently runs `npm test` and `npm run build` for every task even though the planned project is Python-oriented.
- There is no project-level end-to-end regression command that runs after every change.
- Self-review requires a changed file even when Qwen correctly finds nothing to fix.

The target workflow is:

```text
Strong model writes a precise task
              |
              v
Tulid runs the existing E2E baseline
              |
              v
Qwen implements and runs focused + E2E tests
              |
              v
Tulid independently reruns focused + E2E tests
              |
              v
Qwen performs a narrow self-review and fixes concrete defects
              |
              v
Tulid reruns the tests and accepts the task
```

# Prompting changes

## 1. Make the strong model produce an executable task contract

The strong model should continue producing a detailed implementation specification. The derived task given to Qwen should be a smaller execution contract, not another design document.

Every implementation task should state:

- one observable outcome;
- exact files that may be added or edited;
- exact symbols or interfaces involved;
- required behavior and failure behavior;
- invariants that must remain true;
- explicit non-goals;
- focused test commands;
- whether the task only preserves E2E coverage or extends it.

The existing Obsidian frontmatter can carry the fields Tulid needs to enforce:

```yaml
---
local_id: pipeline-error-normalization
dependencies: [pipeline-core]
allowed_paths:
  - src/stt_clipboard/pipeline.py
  - tests/test_pipeline.py
focused_checks:
  - uv run pytest tests/test_pipeline.py -q
e2e_role: regression
---
```

The Markdown body remains useful for the detailed behavior, signatures, Panalyzer identities, rationale, and acceptance criteria. A separate `task-contract.json` is unnecessary for the first version.

### Changes to the planning prompts

Update the implementation-spec and breakdown prompts so that:

- tasks are divided by coherent behavior, not by a fixed number of tasks per module;
- the current “3–4 tasks per module” rule is removed;
- the 80–140-line task target is removed;
- Mermaid diagrams are kept in the implementation specification when useful, but are not mandatory in every Qwen task;
- 500 changed production lines remains an exceptional maximum rather than a target;
- a normal Qwen task aims for roughly 1–3 production files and 50–150 changed production lines;
- every task contains enough interface and test detail that Qwen does not need to make an architectural decision.

Panalyzer should remain a planning tool. The strong model uses it to discover the real files, symbols, signatures, and call edges. Qwen receives only the subset assigned to its task, rather than the complete proposal history.

### What this achieves

The expensive reasoning happens once in the strong model. Qwen does not have to decide where code belongs, invent an interface, or guess how completion will be measured.

## 2. Put Qwen's operative instructions first

The Qwen prompt should begin with a compact execution section. Reference material comes afterward.

Recommended order:

1. Objective.
2. Allowed change surface.
3. Required interfaces and behavior.
4. Focused test commands.
5. Project E2E command.
6. Completion requirements.
7. Reference material.

Example:

```markdown
# Implement one scoped task

## Objective

Normalize STT backend failures through `PipelineError` without changing the
public pipeline interface.

## Allowed changes

- `src/stt_clipboard/pipeline.py`
- `tests/test_pipeline.py`

Do not change CLI parsing, installation, service management, or platform
adapters.

## Required behavior

- Preserve `Pipeline.run(source: AudioSource) -> str`.
- Convert backend failures to `PipelineError`.
- Never return empty text as a successful result.

## Verification

Run in this order:

1. `uv run pytest tests/test_pipeline.py -q`
2. `uv run pytest tests/e2e -q`

Make the smallest coherent change. Submit completion only after both commands
pass.
```

The task body and selected supporting documents follow under a heading such as `Reference material`. That section should explicitly say that it explains the task but does not expand its scope.

### What this achieves

The first information Qwen sees answers the five questions that matter: what must change, where may it change, what must remain stable, how is it tested, and when is it done.

## 3. Reduce prompt noise

The current packet repeats validation-failure guidance and renders the completion `curl` example twice. Consolidate each rule into one location.

For implementation jobs:

- Keep the full current task.
- Replace the full parent body with its title, objective, and relevant acceptance context.
- Include only directly relevant linked documents.
- Continue excluding sibling implementation task artifacts.
- Use a much smaller context limit than the current 512 KB linked-context ceiling.
- Do not introduce semantic summarization in the first version. Use deterministic selection and byte limits.
- If a task cannot be understood within the implementation context budget, reject it as insufficiently decomposed and return it to planning.

Keep the existing OpenCode invocation, local model proxy, and prompt-file mechanism. The change belongs in Tulid's prompt construction and the existing agent Markdown.

### What this achieves

Qwen receives fewer competing instructions. This lowers the chance of replanning, editing unrelated files, or overlooking the acceptance commands.

## 4. Give Qwen a fixed working procedure

The implementation prompt should tell Qwen to use this sequence:

1. Read the objective, allowed paths, and named interfaces.
2. Inspect the named files before editing.
3. Run the narrowest existing focused test when useful to establish the current state.
4. Make the smallest coherent implementation.
5. Run the focused checks.
6. Run the project E2E suite.
7. If a check fails, diagnose that check and make at most two targeted repair attempts.
8. Submit the actual changed files and command results.

If the failure is outside the task boundary or existed before Qwen's change, Qwen should stop and report it instead of broadening the task.

### What this achieves

Qwen follows one predictable implementation loop instead of inventing a new approach for every job.

## 5. Make self-review a different job from implementation

The current self-review prompt is directionally correct but the workflow contradicts it by requiring a changed file.

The review prompt should ask Qwen to:

- reread the task objective and allowed paths;
- inspect the promoted change as if another developer wrote it;
- connect each acceptance criterion to code or test evidence;
- look for concrete correctness errors, missing branches, or broken invariants;
- run the focused checks and E2E suite;
- edit only when it finds a specific in-scope defect;
- submit a no-change completion when the implementation is already correct.

It should not invite general cleanup, new abstractions, or unrelated test expansion.

### What this achieves

The second Qwen pass becomes an evidence-based defect check instead of a repeated implementation pass.

## 6. Validate the prompt change without building new software

Before replacing the default prompt, replay a small fixed set of tasks through the existing `tulid jobs run-one` flow:

- a narrow bug fix;
- a public-interface change;
- a task that must preserve an existing E2E flow;
- an end-to-end task that adds a scenario.

Run each task a few times with the old and new prompt. Compare:

- focused and E2E test results;
- unexpected files changed;
- amount of code changed;
- completion failures;
- elapsed time;
- whether human intervention was required.

Tulid already retains prompt packets, hashes, logs, and events. Those are enough for this first comparison.

### What this achieves

Prompt changes receive a practical quality check without creating a new evaluation product.

# Testing changes

## 1. Treat the E2E suite as a persistent product contract

Each project should define one deterministic E2E command in `workflow.yaml`. Ordinary tasks do not choose or replace this command.

For a Python project such as STT-clipboard:

```yaml
- kind: validation_type
  id: end_to_end_pass
  args:
    command:
      type: string
```

Implementation and review transitions then require:

```yaml
- type: end_to_end_pass
  args:
    command: uv run pytest tests/e2e -q
```

`end_to_end_pass` can reuse Tulid's existing trusted command runner. It needs a separate validation name so its result is visible independently from unit tests and builds.

### What this achieves

Every accepted task proves that it has not broken the user flows the project already knows how to exercise.

## 2. Make focused checks task-specific and trusted

The strong model places narrow commands in the task's `focused_checks` frontmatter.

Add a built-in validation named `task_checks_pass`. It reads those commands from `Task.metadata` and runs them using the existing trusted validation workspace.

Tulid should validate the field before scheduling:

- it must be a non-empty list;
- every entry must be a non-empty command string;
- the rendered Qwen prompt must show the same commands Tulid will later execute.

### What this achieves

The task's detailed testing instructions stop being advisory prose. Qwen runs them while implementing, and Tulid independently enforces the same checks.

## 3. Introduce dedicated end-to-end tasks

Not every task should add E2E functionality. Use two task types:

- `ImplementationTask` changes a narrow component and must preserve the existing E2E suite.
- `EndToEndTask` composes components into a complete flow and adds or extends a durable E2E scenario.

Each task declares an E2E role:

| Role | Meaning | May edit E2E tests? | Pre-task E2E required? | Post-task E2E required? |
|---|---|---:|---:|---:|
| `bootstrap` | Establish the first E2E harness | Yes | No | Yes |
| `regression` | Preserve current flows | No | Yes | Yes |
| `extend` | Add or change a complete flow | Yes | Yes | Yes |

`bootstrap` is valid only when the configured suite does not exist yet.

An end-to-end task should depend on the lower-level tasks needed for that flow:

```text
audio adapter ─┐
STT adapter ───┼──> E2E task: fixture audio to clipboard
pipeline ──────┤
clipboard ─────┘
```

The E2E task owns the final wiring and the scenario. It is accepted only when the new scenario and all prior scenarios pass.

### What this achieves

Small component tasks remain small. New user-visible behavior still acquires durable E2E coverage at a deliberate integration point.

## 4. Run E2E before and after Qwen

Before launching Qwen, Tulid should run the project E2E command in the fresh, unmodified workspace.

- If it passes, record the baseline and launch Qwen.
- If it fails, mark the task as blocked by a baseline failure.
- Do not spend Qwen inference trying to repair unrelated existing breakage.
- Skip this pre-check only for the one explicit E2E bootstrap task.

After Qwen submits its work, Tulid runs:

1. task-specific focused checks;
2. project build or static checks;
3. the complete deterministic E2E suite.

Qwen's reported output is evidence for diagnosis, but Tulid's rerun is authoritative.

If Tulid rejects completion while Qwen is still running, return the failing validation name, command, exit code, and a bounded error excerpt. Qwen can fix the same workspace and resubmit.

### What this achieves

Failures can be attributed more accurately. Qwen receives a useful repair loop, while fabricated or stale test claims cannot advance the task.

## 5. Define E2E at the application boundary, not the hardware boundary

The mandatory per-task E2E suite must be deterministic and runnable inside the project worker image.

For STT-clipboard, the suite should use:

```text
fixture audio
  -> real application configuration and orchestration
  -> fake recorder/audio boundary
  -> fake or fixture-backed STT boundary
  -> fake LLM boundary when required
  -> fake clipboard and hotkey boundaries
  -> asserted text, state, exit status, and error behavior
```

The composition root, configuration loading, application state machine, pipeline, and error normalization should remain real. Only machine-specific or nondeterministic boundaries should be replaced.

Real microphone, Whisper model, global hotkey, desktop clipboard, indicator, and service-manager checks belong in a separate host-smoke suite. Host smoke runs at release milestones or on capable machines, not after every ordinary task.

### What this achieves

Tulid can verify meaningful application behavior on every task without making the gate dependent on hardware, a desktop session, network access, or a local model server.

## 6. Prevent ordinary tasks from weakening the E2E contract

Ordinary implementation tasks should list E2E tests and fixtures as forbidden paths. Only an `EndToEndTask` or an explicit strong-model exception may edit them.

Tulid cannot safely enforce this from Qwen's submitted `changed_files` alone. Worker workspaces omit `.git`, so the verifier should compare the completed workspace against a file-hash manifest captured before Qwen starts.

The manifest comparison should:

- detect added, changed, and removed files;
- exclude `.open-tulid`, output artifacts, and declared build caches;
- compare actual changed paths with the task's `allowed_paths`;
- reject undeclared edits before promotion.

### What this achieves

Qwen cannot make a regression disappear by weakening the test that detects it or by omitting a changed file from its completion payload.

# Process changes

## 1. Make E2E design part of the implementation specification

The strong model's implementation specification should include an E2E strategy:

- the project-owned E2E command;
- user-visible flows already protected;
- new flows or important branches required by this feature;
- the real application entrypoint used by each scenario;
- external boundaries replaced with fakes or fixtures;
- the `EndToEndTask` responsible for each new scenario;
- optional host-smoke coverage.

Panalyzer helps the strong model find the composition root, adapters, callers, and dependency edges. The specification turns that evidence into explicit test and task decisions.

### What this achieves

Testability is designed before Qwen starts implementing. Integration is not postponed until a collection of isolated modules already exists.

## 2. Change task breakdown to converge on working flows

The breakdown should produce:

- narrow component tasks;
- explicit dependencies between them;
- an E2E task after the prerequisites for each complete flow;
- a final E2E acceptance task when several flows must work together.

Every generated task must contain:

- `allowed_paths`;
- `focused_checks`;
- `e2e_role`;
- exact interfaces and behavior;
- acceptance criteria and non-goals.

The breakdown transition should validate these fields before creating runnable child tasks.

### What this achieves

The task graph contains explicit integration milestones. A set of individually completed modules cannot be mistaken for a usable feature.

## 3. Correct the implementation lifecycle

The desired lifecycle is:

```text
Todo
  -> baseline E2E
  -> Qwen implementation
  -> trusted focused/build/E2E validation
  -> Self review
  -> Qwen audit or targeted correction
  -> trusted focused/build/E2E validation
  -> Done
```

Required workflow corrections:

- Remove `changed_files.required: true` from `SelfReview`.
- Keep changed-file promotion active when review actually changes something.
- Replace STT-clipboard's fixed Node commands with commands that match the Python project.
- Require `task_checks_pass` and `end_to_end_pass` for implementation and review.
- Add equivalent implement/review transitions for `EndToEndTask`.

### What this achieves

Implementation must produce a change, but review may legitimately confirm that no correction is needed. Both stages remain gated by real tests.

## 4. Do not mark the parent feature complete after task breakdown

The current default workflow moves the parent product task to `Done` when child tasks are generated. That describes planning completion, not product completion.

Change the parent lifecycle so breakdown moves it to an implementation state. Move it to `Done` only when:

- all required child tasks are complete;
- all required E2E extension tasks are complete;
- the final deterministic E2E suite passes;
- the configured build/package check passes.

If parent-state gating is too large for the first increment, generate one final `EndToEndTask` that depends on every required child. This is an acceptable first approximation, but the parent should eventually reflect actual implementation state.

### What this achieves

“Done” means the planned pieces compose into a runnable result, not merely that the plan was divided into tickets.

## 5. Keep failure handling narrow and explicit

Classify failures into:

- **baseline failure:** the repository was already broken; block before Qwen;
- **implementation failure:** Qwen's change broke a required check; allow up to two targeted repairs;
- **contract failure:** the task cannot be completed inside its allowed surface; return to strong-model planning;
- **environment failure:** required infrastructure is unavailable; block without asking Qwen to edit code.

Record the classification, command, output excerpt, and attempt count in the existing job/event records.

### What this achieves

Qwen spends its limited attempts on defects it can actually fix. Planning and environment problems are not disguised as coding failures.

## 6. Roll out in bounded increments

### Increment 1: Prompt cleanup

- Put the execution contract first.
- Remove duplicated completion and failure instructions.
- Reduce parent and linked implementation context.
- Simplify the task template.
- Make self-review explicitly accept no change.

This is the lowest-risk change and needs no new subsystem.

### Increment 2: Trusted task checks

- Add `allowed_paths`, `focused_checks`, and `e2e_role` to generated task frontmatter.
- Add `task_checks_pass`.
- Validate task metadata before scheduling.
- Render the same commands for Qwen that Tulid will execute.

### Increment 3: Persistent E2E regression

- Add `end_to_end_pass` using the existing command runner.
- Add the E2E bootstrap task.
- Require the E2E command after every implementation and review.
- Add `EndToEndTask` and its transitions.

### Increment 4: Baseline and scope enforcement

- Run E2E before launching Qwen.
- Capture the pre-worker file manifest.
- Enforce actual changed paths against `allowed_paths`.
- Protect E2E files from ordinary tasks.

### Increment 5: Product completion

- Add final E2E acceptance dependencies.
- Hold the parent feature in implementation state.
- Mark it `Done` only after children, build, and complete E2E validation pass.

After each increment, replay the same small task set with Qwen and run Tulid's unit, runtime, and Docker-backed E2E tests.

## Implementation touchpoints

The main Tulid changes are expected in:

- `src/open_tulid/runtime/executor.py` for prompt order and the pre-Qwen baseline;
- `src/open_tulid/runtime/context.py` for implementation-specific context selection;
- `src/open_tulid/runtime/workspaces.py` and `verifier.py` for the file manifest and scope enforcement;
- `src/open_tulid/workflow/implementations.py` for trusted focused and E2E validations;
- default agent templates and the STT-clipboard agent prompts;
- default and STT-clipboard workflows;
- runtime, prompt, workflow, and Docker-backed E2E tests.

## Completion criteria

The improvement is complete when:

- Qwen receives a short execution contract before reference context.
- The strong model defines exact change surfaces, interfaces, focused checks, and E2E roles.
- Every ordinary task preserves the established deterministic E2E suite.
- Dedicated E2E tasks add coverage for new complete flows.
- Qwen runs the checks and Tulid independently reruns them.
- A broken baseline blocks the job before Qwen runs.
- Self-review can succeed without changing files.
- Tulid derives actual changed paths and rejects scope violations.
- The parent feature is not `Done` until its final build and E2E suite pass.
