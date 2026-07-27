# Qwen Task 2 Prompt Analysis

## Scope

This document analyzes the prompt rendered by:

```bash
uv run tulid prompts render STT-clipboard 2 --transition ImplementTask
```

The analyzed file is:

```text
/tmp/qwen-task-2-prompt.md
```

The goal is not merely to shorten the prompt. The goal is to make the Qwen
27B implementation worker more likely to produce a coherent, runnable,
testable piece of software without asking it to make architecture decisions
that should have been made by the larger planning model.

## Executive conclusion

The prompt is not yet a reliable implementation contract.

Its largest problems are:

1. It requires `npm test` and `npm run build` for a Python package task in a
   repository with no `package.json`.
2. The task leaves major implementation decisions to Qwen: build backend, CLI
   library, Python version, entrypoint design, application-context interface,
   placeholder behavior, and exact test layout.
3. Generic runtime policy occupies most of the prompt and repeats several
   times, while the task-specific implementation contract is comparatively
   weak.
4. `DO NOT WRITE MD FILES` conflicts with the task's stated `README.md` change
   surface.
5. The completion protocol and validation commands appear twice, and the
   validation-failure policy appears twice.
6. The rendered preview and an actual historical job have opposite context
   problems:
   - the reconstructed preview omits parent and linked artifact context;
   - the historical execution prompt injected about 69 KB and 9,445 words,
     including entire product, technical, and implementation documents.
7. Tulid can schedule an old task that no longer satisfies the current
   Panalyzer-backed task contract.
8. There is no deterministic preflight proving that the task, repository,
   validation commands, and prompt agree before Qwen is started.
9. The prompt tells Qwen how to report validation, but it does not give Qwen a
   small, correct, task-specific end-to-end invariant set.

The central recommendation is:

> Treat prompt construction as compilation of a validated task contract, not
> concatenation of task text, generic policies, and every available context
> document.

The larger model should decide the design and emit a machine-checkable task
contract. Tulid should validate that contract against the repository and
workflow. Qwen should receive one compact execution packet containing only the
decisions and evidence needed to implement that one task.

## Important caveat about the analyzed file

Task `2` no longer exists in the live Obsidian project. Tulid recovered its body
from job snapshot:

```text
01KSJ9JGX50N014BFPNY2EK850
```

Tulid then combined that old task body with the current workflow and current
agent instruction files.

Therefore, `/tmp/qwen-task-2-prompt.md` is useful for analyzing what Tulid would
compile now, but it is not the byte-for-byte prompt originally given to Qwen.

The original historical prompt is still available at:

```text
/home/rawsteel/.tulid/workspaces/01KSJ9JGX50N014BFPNY2EK850/.open-tulid/prompt-packet.md
```

The difference matters:

| Packet | Lines | Words | Bytes | Main context behavior |
|---|---:|---:|---:|---|
| Current reconstructed preview | 255 | 1,685 | 12,266 | No parent or linked artifact documents |
| Historical executed packet | 2,899 | 9,445 | 69,199 | Full parent plus full product, technical, and implementation documents |

This reveals a separate observability requirement:

- `prompts render` should describe the prompt that would be compiled now.
- Tulid should also support showing the immutable prompt packet from a
  historical job.
- The two must be clearly labeled and must never be silently presented as the
  same thing.

## Prompt anatomy

The current prompt is divided as follows:

| Lines | Source | Words | Purpose |
|---|---|---:|---|
| 1-75 | Tulid runtime template | 558 | Role, priority, paths, failure policy, completion |
| 76-163 | Recovered task body | 469 | Actual task-specific implementation request |
| 164-175 | `default.agent.md` | 63 | Generic repository and scope behavior |
| 176-227 | `qwen-implementation.agent.md` | 456 | Scope and validation behavior |
| 228-255 | Tulid final reminder | 139 | Second copy of completion and validation |

Only 469 of 1,685 words—about 28 percent—are the task itself. The other
72 percent are runtime and agent policies.

Policy overhead is not inherently bad, but this overhead is weak because much
of it is repetitive and some of it conflicts with the task.

## Source map

The important prompt sections come from these sources:

| Prompt content | Current source |
|---|---|
| Role, objective, priority, path rules | `src/open_tulid/runtime/executor.py` |
| First completion contract and curl example | `src/open_tulid/runtime/executor.py` |
| Task body | Historical `.open-tulid/job-context.json` snapshot |
| Default instructions | `STT-clipboard/agents/default.agent.md` |
| Qwen instructions | `STT-clipboard/agents/qwen-implementation.agent.md` |
| Required validation commands | `STT-clipboard/workflow.yaml` |
| Second completion contract | `src/open_tulid/runtime/executor.py` |
| Historical linked context | `src/open_tulid/runtime/context.py` |

This source map shows why simply editing
`qwen-implementation.agent.md` cannot solve the problem. Several of the most
damaging defects originate in the workflow, task generator, runtime template,
and context resolver.

# Detailed findings

## Critical: the validation commands are wrong

The prompt requires:

```text
tests_pass: npm test
project_build: npm run build
```

The task says to create a Python package:

```text
create src/voiceflow_local/
add pyproject.toml
```

The target repository currently contains only:

```text
.gitignore
```

There is no `package.json`, and the Git ignore file itself is primarily a
Python project ignore file.

The wrong commands come directly from the `ImplementTask` and `SelfReview`
transitions in `STT-clipboard/workflow.yaml`.

### Why this damages Qwen

Qwen has no correct way to satisfy the prompt:

- If it follows the task and creates Python code, the mandated validations
  fail.
- If it follows the validation commands, it may invent a Node project that the
  task did not request.
- If it reports the failure as environmental, Tulid's trusted verifier still
  runs the incorrect commands and rejects completion.
- The repeated appearance of the commands makes them look especially
  authoritative.

This is not a model-quality problem. It is an impossible contract.

### Required change

Validation commands must be selected before scheduling and checked against the
task and repository.

For this specific task, a plausible validation set is:

```bash
uv run pytest -q tests/unit/cli
uv run voiceflow-local --help
uv build
```

The exact commands depend on upstream decisions about the package backend and
development dependencies. Those decisions must be made by the planning model,
not guessed by Qwen.

Tulid should reject a prompt before execution when:

- a command requires `package.json` but the task is for a Python package;
- a command's executable or manifest is absent and the task does not explicitly
  create it;
- task-level validation and workflow-level validation disagree;
- a required validation command is not represented in the task contract;
- an end-to-end command cannot exercise the behavior named in the acceptance
  criteria.

## Critical: the task is not implementation-ready

The task describes a broad result, but it does not lock enough decisions for a
27B implementation model.

Qwen must currently decide:

- supported Python version;
- build backend;
- dependency and development dependency layout;
- whether to use `argparse`, Click, Typer, or another CLI framework;
- entrypoint module and callable signature;
- command registration structure;
- `ApplicationContext` shape and ownership;
- what every unimplemented command does;
- whether placeholders exit successfully or fail explicitly;
- how tests invoke the CLI;
- exact install and build commands;
- which files under `src/voiceflow_local/cli/commands/` must exist;
- whether `README.md` is actually required.

These are architecture and contract decisions. The high-level model was
supposed to make them.

### Evidence in the task

The task uses phrases such as:

- "files or components likely touched";
- "reserve a shared application-context object";
- "minimal help renderer";
- "stub handlers";
- "package installs".

These phrases constrain intent but do not define exact interfaces or observable
behavior.

The current Qwen instruction says to preserve:

- module boundary;
- allowed change surface;
- primary symbols and contracts;
- proposal files and methods.

But the recovered task contains none of those fields in the current required
format.

### Required change

Every implementation task must contain:

- a task-contract schema version;
- exact owned behavior;
- exact files to add;
- exact files allowed to edit;
- forbidden files or directories;
- exact public symbols and signatures;
- explicit invariants and failure behavior;
- concrete acceptance criteria;
- exact targeted tests;
- exact trusted validation commands;
- the project end-to-end invariant commands that must remain green;
- exact Panalyzer proposal entities covered;
- the expected maximum change size.

If these fields are absent, Tulid should not send the task to Qwen. It should
route the task back to the large-model planning or grooming stage.

## Critical: task contract age is not checked

The current breakdown process is substantially stricter than the recovered
task. Current task templates require:

- vertical slice ownership;
- proposal packages, files, methods, and references;
- spec behavior;
- explicit module boundary;
- exact allowed change surface;
- symbols and signatures;
- non-goals;
- exact validation;
- a Mermaid boundary diagram.

The old task predates that contract, but Tulid can still reconstruct and render
it with current Qwen instructions.

This produces a false appearance of compatibility: new instructions refer to
fields that the old task never defined.

### Required change

Add a contract version to derived tasks:

```yaml
contract_version: 2
planner_run_id: ...
implementation_spec_sha256: ...
panalyzer_proposal_sha256: ...
```

Each implementation transition should declare the minimum accepted contract
version.

Before scheduling, Tulid should validate:

1. contract version is supported;
2. required sections are present and non-empty;
3. referenced Panalyzer identities exist;
4. validation commands agree with the workflow;
5. linked inputs still match their recorded hashes;
6. task dependencies are complete;
7. repository baseline is compatible with the task assumptions.

Old or stale tasks can still be inspected, but they should be marked
`not execution-ready`.

## Critical: Markdown instructions conflict

The Qwen instruction ends with:

```text
DO NOT WRITE MD FILES.
ONLY WRITE IMPLEMENTATION AS DESCRIBED IN THE GIVEN TASK
```

The task lists:

```text
README.md
```

as a likely changed file.

The instruction also conflicts with future implementation tasks that may
legitimately own documentation, configuration examples, migration notes, or
release evidence.

### Required change

Replace the blanket prohibition with:

> Do not create planning summaries, implementation reports, or other
> unrequested Markdown artifacts. Edit an existing Markdown file only when the
> task's allowed change surface explicitly includes it and an acceptance
> criterion requires the change.

Even better, remove `README.md` from this task unless a concrete acceptance
criterion says exactly what must be documented.

## High: completion instructions are duplicated

The full curl submission appears at lines 58-71 and again at lines 233-246.

The following also appear twice:

- required validations;
- validation commands;
- changed-file requirement;
- do not exit before completion;
- wait for in-progress completion;
- fix and resubmit after rejection.

### Why this is harmful

Duplication:

- spends context on protocol instead of implementation facts;
- pushes the task away from the end of the prompt;
- gives the model two places to reconcile if the protocol changes;
- increases the chance that one copy becomes stale;
- makes the prompt feel more threatening than actionable.

### Required change

Keep one compact statement near the beginning:

> Completion is accepted only after Tulid verifies the workspace and receives
> the completion submission.

Keep one full, generated completion example at the very end.

Prefer generating an explicit evidence object:

```json
{
  "summary": "Implemented package scaffold and CLI registration.",
  "artifacts": [],
  "changed_files": ["pyproject.toml", "src/voiceflow_local/cli/main.py"],
  "validation_evidence": {
    "tests_pass": "uv run pytest -q tests/unit/cli: exit 0",
    "project_build": "uv build: exit 0",
    "cli_help_smoke": "uv run voiceflow-local --help: exit 0"
  }
}
```

Do not use a generic `"validation-id"` key when the required IDs are already
known.

## High: validation-failure policy is duplicated

Tulid's runtime template contains a validation-failure policy at lines 29-36.
The Qwen instruction repeats a longer version at lines 203-222.

Several sentences are identical or nearly identical:

- use failures as diagnosis;
- determine whether the failure is in scope;
- switch to the narrowest failing command;
- stop after two targeted attempts;
- do not repair environmental or unrelated failures.

### Required change

Own this policy in one place.

The runtime-generated version is preferable because Tulid can tailor it to the
transition. Remove it from `qwen-implementation.agent.md`, or reduce the agent
file to implementation-specific behavior that the runtime does not already
provide.

## High: scope rules are repeated instead of compiled

The same scope idea occurs in:

- Role;
- Primary Objective;
- Context Priority;
- Default Agent Instructions;
- Qwen Implementation;
- Validation Failure Policy;
- uppercase final warning.

Repetition cannot compensate for the absence of an exact allowlist.

### Required change

Replace generic scope warnings with a generated task boundary:

```text
Allowed additions:
- pyproject.toml
- src/voiceflow_local/__init__.py
- src/voiceflow_local/cli/__init__.py
- src/voiceflow_local/cli/main.py
- tests/unit/cli/test_main.py

Allowed edits:
- none; repository is a bootstrap baseline

Forbidden:
- installer behavior
- config parsing
- daemon lifecycle
- STT/LLM/clipboard implementation
- README.md unless a documentation criterion is added
```

One exact allowlist is stronger than many generic warnings.

## High: the prompt lacks repository facts

The current repository contains only `.gitignore`, but the prompt does not say
that.

For a bootstrap task this is essential context. Without it, Qwen may:

- spend time searching for files that do not exist;
- infer that the workspace copy failed;
- design compatibility with nonexistent code;
- create too much structure because no baseline is stated;
- treat missing manifests as an environment failure.

### Required change

Tulid should generate a small repository-facts section immediately before the
task contract:

```text
Repository baseline:
- Git repository: yes
- Tracked files: .gitignore only
- pyproject.toml: absent; this task owns its creation
- package.json: absent and not part of this task
- src/voiceflow_local/: absent; this task owns its creation
- tests/: absent; this task owns the listed test files
- Working tree state at job creation: clean
```

This is deterministic context. It should come from Tulid, not from a model.

## High: context is either missing or excessive

The reconstructed preview has no parent or linked reference documents because
the historical job snapshot does not preserve:

- `parent_id`;
- `artifact_links`;
- task metadata used by context selection.

The original historical prompt has the opposite problem. It contains:

- an enormous parent body;
- the full Product Spec;
- the full Technical Direction;
- the full Implementation Spec;
- repeated parent headings;
- instruction documents.

The original packet is 69,199 bytes and 9,445 words for a package-scaffold
task. The task only references a few specific sections.

### Why the historical context is poor for Qwen

The model must search thousands of words for a few relevant decisions.
Broad product requirements can appear to broaden the implementation task.
Repeated headings and overlapping documents create false salience.

The high-level model has already read and synthesized these documents. Passing
all of them to Qwen delegates planning back to the implementation model.

### Required change

The planning model should create a self-contained task capsule.

Qwen should receive:

1. the exact task contract;
2. the exact Panalyzer proposal slice owned by the task;
3. small, explicitly selected excerpts only when the task cannot be
   self-contained;
4. generated repository facts.

Do not inject the entire parent task or every parent artifact by default.

Suggested starting budgets:

| Content | Starting budget |
|---|---:|
| Task contract | 1,500-2,500 words |
| Repository facts | 100-300 words |
| Panalyzer slice | 200-800 words |
| Linked excerpts | At most 2,000 words total |
| Runtime and completion policy | At most 500 words |

These are initial engineering limits, not universal model limits. They should
be tuned using measured Qwen outcomes.

### Snapshot completeness

`job-context.json` should persist the complete canonical task fields:

- `parent_id`;
- `artifact_links`;
- dependencies;
- metadata;
- contract version;
- source hashes.

For historical inspection, Tulid should retain:

- the immutable executed prompt;
- the task snapshot;
- instruction document hashes;
- context document hashes;
- workflow hash;
- repository SHA.

## High: task ordering is not optimized for a smaller model

The prompt begins with 75 lines of runtime behavior before showing the task.
The task is then followed by another 64 lines of policy and another completion
block.

The most important content is neither first nor last.

### Required change

Use this order:

1. Mission in one paragraph.
2. Repository facts.
3. Exact task contract.
4. Panalyzer structural slice.
5. Required verification commands.
6. Short execution procedure.
7. One completion contract.

Keep the actual task and concrete facts before generic behavioral guidance.

## Medium: audit metadata is sent to the model

Instruction sections contain:

- absolute host paths;
- SHA-256 values;
- instruction reference headings.

These values are useful for Tulid audit logs but do not help Qwen implement the
task.

### Required change

Keep paths and hashes in a sidecar manifest:

```text
.open-tulid/prompt-manifest.json
```

The model prompt should contain only the normalized instruction content and,
when useful, a short logical source label such as `project implementation
policy`.

## Medium: acceptance criteria are not executable enough

Examples:

- "The package installs";
- "Command dispatch can call stub handlers without import cycles";
- "Help output includes every required top-level subcommand".

These can be improved into exact observations:

```text
- `uv build` exits 0 and creates both wheel and sdist.
- `uv run voiceflow-local --help` exits 0 and lists the ten required commands.
- `uv run pytest -q tests/unit/cli/test_main.py` exits 0.
- importing `voiceflow_local.cli.main` succeeds in a fresh Python process.
- every placeholder command returns the planner-selected placeholder exit code
  and message.
```

The last criterion requires the planning model to select the placeholder
behavior.

## Medium: the task is not a strong vertical slice

The task combines:

- package metadata;
- entrypoint wiring;
- CLI parser;
- ten command registrations;
- application-context design;
- placeholder handlers;
- tests;
- possibly README changes.

This is plausible as a bootstrap task, but it still leaves many independent
choices.

Two good options are:

1. Keep one bootstrap task but specify exact files, signatures, placeholder
   behavior, and commands.
2. Split it into:
   - package/build scaffold;
   - CLI root and command registration;
   - application-context interface and dispatch tests.

For Qwen 27B, the second option will usually be easier to verify and repair.

## Medium: no explicit end-to-end invariant

The prompt asks for unit tests and a smoke test, but the workflow only names
`tests_pass` and `project_build`, both incorrectly mapped to npm.

The purpose of the task is to establish a runnable CLI. Its required
end-to-end invariant should therefore be explicit:

```bash
uv run voiceflow-local --help
```

Later tasks should continue running this invariant so they cannot break the
CLI entrypoint while implementing unrelated behavior.

This illustrates a scalable model:

- project-level invariant tests run for every implementation task;
- a task may add new invariant tests when it introduces a new end-to-end flow;
- task-specific targeted tests prove the local change;
- Tulid executes all required commands itself before accepting completion.

Qwen may run the commands during implementation, but Qwen's claim is not the
proof. Tulid's trusted verifier is the proof.

# Proposed prompt design

## Design principles

The Qwen prompt should be:

- concrete rather than motivational;
- task-specific rather than project-comprehensive;
- decision-complete rather than suggestion-heavy;
- generated from validated structured data;
- ordered by implementation usefulness;
- free of duplicate policy;
- paired with trusted validation;
- explicit about what Tulid already verified;
- explicit about what Qwen must make true.

## Recommended prompt schema

```text
# Mission

# Repository Facts

# Task Contract
## Outcome
## Allowed Change Surface
## Forbidden Change Surface
## Symbols and Interfaces
## Required Behavior
## Failure Behavior
## Acceptance Criteria

# Panalyzer Proposal Slice

# Required Validation
## Targeted Checks
## Project Invariants

# Execution Procedure

# Completion Submission
```

Tulid should generate this schema from structured task data rather than
concatenating arbitrary Markdown documents.

# Example of a substantially better prompt

The example below demonstrates the required precision. It makes several
decisions that are absent from the current task. Those decisions are shown to
illustrate what the large planning model must lock before Qwen runs; Tulid
should not silently invent them.

```md
# Mission

Create the initial installable Python package and a thin CLI root for
`voiceflow-local`. This is a bootstrap task in an otherwise empty repository.
Do not implement command business logic.

# Repository Facts

- The Git repository currently tracks only `.gitignore`.
- `pyproject.toml`, `src/`, and `tests/` do not exist yet.
- This task intentionally creates the first package and test files.
- This is a Python project. Do not create `package.json` or Node tooling.
- The working tree was clean when Tulid created this job.

# Locked Design Decisions

- Python requirement: `>=3.11`.
- Build backend: Hatchling.
- CLI parser: Python standard-library `argparse`.
- Console script: `voiceflow-local = voiceflow_local.cli.main:run`.
- `run(argv)` returns an integer exit code.
- `main(argv)` delegates to `run(argv)` and raises `SystemExit` with that code.
- Unimplemented subcommands must print
  `<command>: not implemented` to stderr and return exit code `2`.
- Development test dependency: pytest.

# Task Contract

## Outcome

After installation, `voiceflow-local --help` must run successfully and list:

- `install`
- `start`
- `stop`
- `restart`
- `status`
- `once`
- `test`
- `bench`
- `doctor`
- `config`

Every listed command must dispatch through one shared command registry.

## Allowed additions

- `pyproject.toml`
- `src/voiceflow_local/__init__.py`
- `src/voiceflow_local/cli/__init__.py`
- `src/voiceflow_local/cli/main.py`
- `src/voiceflow_local/cli/context.py`
- `src/voiceflow_local/cli/commands/__init__.py`
- `src/voiceflow_local/cli/commands/placeholders.py`
- `tests/unit/cli/test_main.py`

## Allowed edits

- `.gitignore` only if a generated Python or build path is demonstrably
  missing from the existing rules.

## Forbidden changes

- Do not implement configuration parsing.
- Do not implement daemon or service lifecycle.
- Do not implement STT, LLM, recording, clipboard, hotkey, or indicator logic.
- Do not create installer scripts.
- Do not edit or create Markdown documentation in this task.
- Do not introduce a second CLI framework or a plugin system.

## Required symbols

```python
@dataclass(frozen=True)
class ApplicationContext:
    """Dependency container reserved for later command implementations."""

def build_parser() -> argparse.ArgumentParser:
    """Return the complete root parser with all required subcommands."""

def run(
    argv: Sequence[str] | None = None,
    *,
    context: ApplicationContext | None = None,
) -> int:
    """Parse arguments and invoke the selected command handler."""

def main(argv: Sequence[str] | None = None) -> NoReturn:
    """Exit the process with the code returned by run()."""
```

The command registry must map each command name to a handler callable. Parser
construction must not import future runtime subsystems.

## Required behavior

1. Root `--help` returns `0`.
2. Root help lists all ten commands.
3. `<command> --help` returns `0` for every command.
4. Invoking an unimplemented command returns `2` and prints the exact
   placeholder message.
5. Importing the CLI module has no filesystem, network, subprocess, or
   environment mutation side effects.
6. `run()` is testable without spawning a subprocess.

## Acceptance criteria

- `uv build` exits `0` and produces wheel and source distributions.
- `uv run voiceflow-local --help` exits `0`.
- The help output contains every required command exactly once.
- `uv run voiceflow-local status` exits `2` and writes
  `status: not implemented` to stderr.
- `uv run pytest -q tests/unit/cli/test_main.py` exits `0`.
- Tests cover root help, command enumeration, one placeholder dispatch, and
  import safety.

# Panalyzer Proposal Slice

This bootstrap task owns these additions:

- package: `voiceflow_local`
- package: `voiceflow_local.cli`
- file: `src/voiceflow_local/cli/main.py`
- file: `src/voiceflow_local/cli/context.py`
- method: `voiceflow_local.cli.main.build_parser`
- method: `voiceflow_local.cli.main.run`
- method: `voiceflow_local.cli.main.main`

Do not implement proposal entities assigned to later tasks.

# Required Validation

Run the narrow check first:

```bash
uv run pytest -q tests/unit/cli/test_main.py
```

Then run the task end-to-end smoke checks:

```bash
uv run voiceflow-local --help
uv run voiceflow-local status
```

Finally run the trusted transition checks:

```bash
uv run pytest -q
uv build
```

Tulid will rerun the trusted checks. Do not report success if any required
command fails.

# Execution Procedure

1. Inspect `.gitignore` and confirm the stated empty baseline.
2. Create only the allowed files.
3. Implement the parser and dispatch contracts.
4. Add focused tests before broad validation.
5. Compare `git diff --name-only` with the allowed change surface.
6. Check every acceptance criterion against actual output.
7. Submit completion once, using every changed file and concrete evidence for
   every validation ID.

# Completion Submission

Submit:

```json
{
  "summary": "Created the installable Python package and CLI command registry.",
  "artifacts": [],
  "changed_files": [
    "pyproject.toml",
    "src/voiceflow_local/__init__.py",
    "src/voiceflow_local/cli/__init__.py",
    "src/voiceflow_local/cli/main.py",
    "src/voiceflow_local/cli/context.py",
    "src/voiceflow_local/cli/commands/__init__.py",
    "src/voiceflow_local/cli/commands/placeholders.py",
    "tests/unit/cli/test_main.py"
  ],
  "validation_evidence": {
    "tests_pass": "uv run pytest -q: exit 0",
    "project_build": "uv build: exit 0",
    "cli_help_smoke": "uv run voiceflow-local --help: exit 0"
  }
}
```

Use Tulid's generated completion endpoint and token. If Tulid rejects the
submission, fix only the reported in-scope defect and resubmit.
```

## Why the example is better

The example:

- states the empty repository baseline;
- removes Node ambiguity;
- locks language, build, CLI, and entrypoint choices;
- converts a vague file list into an allowlist;
- defines public signatures;
- defines placeholder behavior;
- makes acceptance criteria executable;
- gives Qwen a narrow implementation order;
- includes a real end-to-end smoke test;
- includes only the Panalyzer slice owned by the task;
- includes completion once;
- avoids broad product and architecture documents.

The important improvement is not tone. It is the reduction of unresolved
decisions.

# Process changes

## P0: correct validation before any more Qwen runs

Change `ImplementTask` and `SelfReview` away from the current npm commands.

Do not merely replace npm with one global Python command. The process should
support task- and project-specific validation sets:

```yaml
project_invariants:
  - id: package_build
    command: uv build
  - id: cli_help_smoke
    command: uv run voiceflow-local --help

task_validations:
  - id: tests_pass
    command: uv run pytest -q tests/unit/cli/test_main.py
```

What it achieves:

- removes impossible completion contracts;
- makes test evidence meaningful;
- lets every task preserve established end-to-end flows;
- lets new tasks add an invariant when they create a new flow.

## P0: add an implementation-readiness gate

Before scheduling Qwen, Tulid should validate the task contract.

Minimum checks:

- required task sections exist and are non-empty;
- allowed and forbidden change surfaces exist;
- exact validation commands exist;
- acceptance criteria map to at least one test or command;
- Panalyzer proposal references resolve;
- task contract version is accepted;
- task input hashes match current planning artifacts;
- current repository facts do not contradict the task;
- the prompt contains no unresolved template markers;
- the prompt contains no contradictory commands.

If readiness fails, route the task back to the large planner with the exact
diagnostics.

What it achieves:

- Qwen only receives tasks that are implementable;
- failures become planner feedback instead of low-quality code;
- stale tasks cannot silently use newer incompatible prompts;
- repeated local-model retries are avoided.

## P0: remove runtime and agent duplication

Use the runtime as the single owner of:

- validation failure policy;
- completion mechanics;
- changed-file evidence;
- retry behavior.

Use `qwen-implementation.agent.md` only for Qwen-specific implementation
behavior:

- inspect before editing;
- use exact allowed surfaces;
- implement one vertical slice;
- add focused tests;
- compare the final diff to the contract.

What it achieves:

- shorter prompt;
- one source of truth;
- less instruction competition;
- safer future completion-protocol changes.

## P0: fix the Markdown rule

Replace the blanket ban with a task-aware artifact rule.

What it achieves:

- removes a direct contradiction;
- still prevents Qwen from producing unwanted reports;
- permits legitimate documentation tasks.

## P1: compile repository facts

At workspace preparation time, Tulid should generate a compact facts object:

```json
{
  "git_sha": "...",
  "dirty": false,
  "manifests": [".gitignore"],
  "languages": ["python-intended"],
  "owned_paths": {
    "existing": [],
    "missing": ["pyproject.toml", "src/voiceflow_local"]
  },
  "test_entrypoints": [],
  "build_entrypoints": []
}
```

Render only the relevant facts into the model prompt.

What it achieves:

- reduces exploratory tool use;
- distinguishes intentional bootstrap work from missing files;
- lets the readiness gate detect command/toolchain contradictions;
- grounds Qwen in actual workspace state.

## P1: make Panalyzer a task-boundary source, not prompt bulk

The large model should use Panalyzer during specification and breakdown.
Qwen should receive only the proposal entities assigned to its task.

Required process:

1. Large model produces implementation spec and valid Panalyzer proposal.
2. Tulid validates the proposal.
3. Large model derives tasks that reference exact proposal entity IDs.
4. Tulid validates that every reference exists.
5. Tulid injects only the current task's proposal slice.
6. Completion validation checks that changed production files are compatible
   with the declared slice, allowing explicitly listed tests and package
   metadata.

What it achieves:

- preserves structural precision;
- avoids asking Qwen to reinterpret a full proposal;
- reduces scope wandering;
- makes unexpected file changes detectable.

For a bootstrap repository, Panalyzer cannot describe existing code, but the
proposal can still describe intended additions. The task must explicitly say
that its proposal slice contains additions against an empty baseline.

## P1: replace full parent injection with task capsules

Stop automatically injecting the full parent body and all parent artifact
contents into implementation prompts.

Instead, the breakdown model should copy all required decisions into the task
contract and provide explicit context references such as:

```yaml
context_excerpts:
  - artifact: ImplementationSpec
    section: Repository target shape
  - artifact: ImplementationSpec
    section: CLI contracts
```

Tulid should extract only those sections, enforce a budget, and show the
selected excerpts in `prompts render`.

What it achieves:

- smaller and more relevant prompts;
- fewer broad requirements that look like implementation scope;
- predictable prompt size;
- better observability of why each context item was included.

## P1: separate implementation and self-review packets

The self-review transition should not simply give Qwen another broad
implementation prompt.

Its packet should include:

- the original compact task contract;
- changed-file list;
- diff summary;
- trusted validation results;
- failed or unproven acceptance criteria;
- project end-to-end invariants;
- instructions to make only targeted corrections.

The self-review worker should follow:

1. contract-to-diff comparison;
2. acceptance-criterion checklist;
3. targeted defect inspection;
4. narrow tests;
5. full invariant run;
6. completion.

What it achieves:

- makes the second Qwen pass genuinely different;
- concentrates context on the produced code;
- reduces repeated implementation from scratch;
- improves convergence instead of adding another generic pass.

## P1: keep trusted end-to-end invariants outside model discretion

Add a project-level invariant registry to the workflow.

Rules:

- Every implementation and self-review task runs all established invariants.
- A task that introduces a new end-to-end flow adds its invariant to the
  registry only after that flow passes.
- Tulid runs the invariant commands in the trusted verifier.
- Qwen receives the exact commands and failure output.
- Qwen may fix an invariant only when the failure is caused by its task.

For the scaffold task, the first invariant is:

```bash
uv run voiceflow-local --help
```

Later examples might include one-off raw text, daemon status, or another stable
flow, but each must be introduced by a task that owns and proves it.

What it achieves:

- unrelated tasks cannot silently break existing user flows;
- no single task must implement the whole product end to end;
- the executable definition of "still works" grows with the product.

## P2: add prompt lint and explanation

Extend prompt inspection with:

```bash
tulid prompts render PROJECT TASK --transition ImplementTask
tulid prompts explain PROJECT TASK --transition ImplementTask
tulid prompts lint PROJECT TASK --transition ImplementTask
```

`explain` should report:

- section source;
- byte and token estimate;
- instruction hashes;
- context documents and selection reason;
- repository-fact source;
- live versus historical task source;
- duplicate sentence or block warnings.

`lint` should detect:

- repeated completion blocks;
- repeated validation commands;
- conflicting file permissions;
- workflow/task validation disagreement;
- unresolved placeholders;
- absent required task-contract fields;
- excessive context;
- referenced files that do not exist;
- manifest/validation toolchain mismatch.

What it achieves:

- makes prompt quality inspectable before spending model time;
- turns prompt regressions into test failures;
- makes duplication obvious.

## P2: measure Qwen outcomes instead of prompt aesthetics

Use a fixed replay set of representative tasks and lock:

- Qwen model build;
- quantization;
- context size;
- temperature and sampling settings;
- repository starting SHA;
- task contract;
- Tulid version.

Compare old and new prompt compilers using:

- first-attempt trusted validation pass rate;
- final validation pass rate;
- project invariant pass rate;
- completion rejection count;
- changed-files mismatch rate;
- out-of-scope changed-file rate;
- number of retries;
- wall time;
- tokens or prompt bytes;
- human usability review;
- defect count found in self-review;
- defects escaping self-review.

The most important metric is:

> Does the resulting repository contain a runnable, legitimately usable
> increment that preserves all established project invariants?

# Concrete implementation plan

## Phase 1: immediate corrections

1. Replace the npm validation commands in the STT workflow.
2. Remove duplicated validation policy from
   `qwen-implementation.agent.md`.
3. Replace `DO NOT WRITE MD FILES` with a task-aware rule.
4. Remove one of the two completion blocks.
5. Put the task contract before generic policy.
6. Generate explicit validation-evidence keys.

Expected result:

- no direct contradictions;
- materially shorter prompt;
- correct trusted verification;
- lower completion-submission error rate.

## Phase 2: readiness and task quality

1. Add task contract versioning.
2. Add an implementation readiness validator.
3. Require exact files, symbols, commands, and acceptance criteria.
4. Validate Panalyzer proposal references.
5. Route rejected tasks back to the large planner.

Expected result:

- Qwen stops receiving tasks that require architecture invention;
- planner quality becomes enforceable;
- stale tasks are detected before execution.

## Phase 3: prompt compiler and context control

1. Represent prompt sections as structured objects with source metadata.
2. Deduplicate policy by semantic role.
3. Add repository facts.
4. Inject only task-selected context excerpts.
5. Enforce per-section and total context budgets.
6. Store audit metadata in a sidecar manifest instead of the prompt.
7. Preserve complete canonical task snapshots.

Expected result:

- stable, explainable prompts;
- high task-information density;
- no 69 KB accidental context dumps;
- accurate historical reconstruction.

## Phase 4: end-to-end protection and self-review

1. Add the project invariant registry.
2. Run invariants after every implementation and self-review transition.
3. Give self-review the diff and validation results.
4. Require acceptance-criterion evidence.
5. Add invariants only through tasks that introduce and prove a new flow.

Expected result:

- each task remains narrow;
- the whole product becomes progressively more protected;
- Qwen can repair its own defects with focused evidence;
- Tulid, not Qwen, decides whether the code really works.

## Phase 5: evaluation

1. Build a fixed task replay corpus.
2. Record rendered prompt and prompt manifest.
3. Run old and new prompt variants under identical model settings.
4. Compare trusted outcome metrics.
5. Keep prompt changes only when they improve runnable results.

Expected result:

- prompt evolution becomes empirical;
- improvements are attributable;
- large rewrites do not rely on intuition alone.

# Recommended acceptance tests for Tulid's prompt compiler

Add tests proving:

- a Python task cannot compile with npm validations unless it explicitly owns a
  Node component;
- a task missing required contract fields is not scheduler-ready;
- the completion curl block appears exactly once;
- each required validation command appears exactly once;
- Markdown permission rules cannot conflict with the allowed change surface;
- task content appears before generic agent policy;
- instruction source paths and hashes are absent from model text but present
  in the prompt manifest;
- parent artifacts are not injected without explicit task context selection;
- context limits are enforced;
- historical task fallback reports incomplete snapshot fields;
- the full canonical task is written to `job-context.json`;
- project invariants run in the trusted verifier;
- self-review receives diff and validation evidence;
- `prompts lint` catches the defects found in this prompt.

# Prioritized recommendation

If only three changes are implemented first, use this order:

1. **Validate the task, repository, and commands before Qwen runs.**
2. **Make the large model emit an exact versioned implementation contract.**
3. **Compile one compact prompt with task-specific context and one completion
   protocol.**

Those changes will yield more than adding stronger language such as
`IMPORTANT`, repeating scope warnings, or asking Qwen to reason harder.

Qwen can implement a precise contract. It cannot reliably repair an
inconsistent contract while also bootstrapping a repository and proving the
result end to end.
