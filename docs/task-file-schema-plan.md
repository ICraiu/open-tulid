# Task File Schema Plan

**Updated:** 2026-09-06 — agreed in discussion; implementation not started

## Purpose

Give every task file one fixed shape so the local model always receives the
same kind of instruction. The template matches how work is already organised
in the team:

```
# Title

<one-or-two-sentence task description, from a production point of view>

## Why
<why the task is needed; what piece of the bigger picture it fills in>

## What
<what actually changes: add / modify / remove, with repository paths>

## How
<high-level direction only: which seam or component to touch. The model
decides the implementation; this section just points it in the right
direction>

## Acceptance
- the tests to run (the machine checks)
- the task's own criteria: "the task is accepted if it does X/Y/Z"
```

One acceptance criterion is always present: **the whole global test suite
passes**. That criterion is enforced by the existing machine machinery
(`acceptance.yaml` + the project contract), so it is written by the authoring
agent as a declaration of the tests to run, never as a substitute for the
task's own criteria.

## What the schema enforces

We agreed that a schema can only enforce *shape and presence*, never quality
of fill. The validator only checks presence; how well a section is filled is
a prompt-side matter for the authoring agent, and how correct it is is
checked by the existing LLM review step in the process.

The validator gains presence-only diagnostics, following the existing
`task.*` code style used by `repair_project` / `validate`:

- `task.section_missing` — a task is missing one of the required
  `<description>`, `## Why`, `## What`, `## How`, `## Acceptance` parts;
- `task.section_empty` — a required section is present but empty;
- `task.acceptance_criteria_missing` — the `## Acceptance` section has no
  per-task machine-checkable acceptance block.
- `task.acceptance_run_unknown` — the machine block names a test command
  that the project never declared (a declared check id or an existing
  profile reference is required and must resolve).

The machine-checkable acceptance block is optional but, when present, must
resolve:

```
## Acceptance
accepts: [<declared profile id or check id>]   # runs the named tests
accepts_if:
  - <criterion one>          # runs the machine command it carries
  - <criterion two>          # e.g. a named test file / exit code
run: [<optional extra deterministic command>]
```

When the machine block is absent, the task keeps the free-prose acceptance
criteria it already has; the LLM review step checks those. Nothing about the
already-verified machine checks changes.

## Prompt-side rules (not schema-enforced)

The authoring agents gain one shared rule in their instruction files
(`agents/task-breakdown.agent.md`,
`agents/implementation-spec-template.agent.md`, and the review step used at
runtime):

> Each additional acceptance criterion gets a foot in the product and a foot
> in the technical: one line phrased in plain language about what the outcome
> should be, and one line naming the exact test, command, or exit code that
> decides it.

The peon implementation prompt (`agents/peon-implementation.agent.md`) and
the self-review prompt keep their existing steps (run the narrowest relevant
check first, then every required project-level validation) and gain the same
rule so the model reads the machine block first and treats the prose as
guidance the review step validates.

## Migration

The existing todo task files in the vault are migrated mechanically:

1. `# Title` and the first sentence of the body become the description;
2. the body's prose paragraphs are split into `## Why`, `## What`, `## How`
   with the existing text moved verbatim;
3. the existing `Acceptance checks:` (or equivalent) bullet list becomes the
   `## Acceptance` section, with the already-machine-verified checks moved
   into `accepts:` / `accepts_if:` / `run:` where a declared check exists,
   and free-prose criteria kept verbatim where no machine check exists.

No task is rewritten or re-scoped; only its structure changes. `tulid
validate` is green at the end of the pass.

## Verification

- Unit tests for the new presence-only diagnostics follow the existing style
  in `tests/adapters/test_obsidian_adapter.py`: a five-part task file
  validates; each missing or empty part fails with its `task.*` code; an
  unparseable or unresolved machine block fails with
  `task.acceptance_run_unknown`.
- The existing end-to-end and validator suites still pass; the 13 todo tasks
  in the vault carry the five-part structure after migration.

## Deferred (agreed)

- Machine verification of each per-task acceptance criterion by a
  deterministic test selector was tried earlier, needs polishing, and is set
  aside; per-task machine blocks carry the declared test commands now, but
  running the full set is a future pass.
- A prompt lint that warns when a machine-checkable criterion has "neither
  foot" (no product sentence and no machine command) is a possible follow-up.
