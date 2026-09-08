# Task File Schema Plan

**Updated:** 2026-09-06 — agreed in discussion; implementation not started

> **Superseded (plan 3, step 3E).** This document guided the earlier `accepts:` /
> `accepts_if:` / `run:` machine-checkable acceptance block and per-task command
> selection. That mechanism is removed for new and migrated tasks: verification
> commands are global at the project level (`contract.yaml`) and inherited by
> every task. The five-part body shape below (`# title`, description,
> `## Why`, `## What`, `## How`, `## Acceptance`) is retained, but acceptance is
> **prose-only** — no `run:`/`accepts:`/`accepts_if:` selector and no independent
> per-task executable command list. The sections describing the machine block
> and its `accepts:` / `run:` selectors are flagged as superseded below.

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
- the task's own criteria: "the task is accepted if it does X/Y/Z"
```

One acceptance criterion is always present: **the whole global test suite
passes**. That criterion is enforced by the existing machine machinery (the
project `contract.yaml` + the global verification policy), and it is **not**
written as a per-task machine block. The authoring agent records product/test
expectations as prose in `## Acceptance`.

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
  prose acceptance criterion.
- `task.acceptance_run_forbidden` — the `## Acceptance` section declares a
  per-task command selector (`accepts:`, `accepts_if:`, `run:`), which is
  not allowed; verification commands are global at the project level.

> ***Superseded machine block.*** The older `accepts:` / `accepts_if:` / `run:`
> machine-checkable acceptance block is removed. `accepts:` no longer resolves
> against declared profile/check ids and per-task `run:` command lists are never
> allowed. Migrate any existing machine block into prose test expectations and
> keep project commands in `contract.yaml`.

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
    `## Acceptance` section with the existing behavioral criteria moved
    verbatim as prose. Any legacy `accepts:` / `accepts_if:` / `run:`
    command selection is removed: project verification commands live only in
    `contract.yaml`, so a command bullet is kept as prose only where its
    underlying test expectation is explicit, and otherwise flagged for review
    rather than invented.

> ***Superseded migration mechanics.*** The earlier plan converted already
> machine-verified checks into `accepts:` / `accepts_if:` / `run:` machine
> blocks. That step is now part of the migration *removal*: command selectors
> are deleted from migrated tasks (see the banner at the top of this file).

No task is rewritten or re-scoped; only its structure changes. `tulid
validate` is green at the end of the pass.

## Verification

- Unit tests for the new presence-only diagnostics follow the existing style
  in `tests/adapters/test_obsidian_adapter.py`: a five-part task file
  validates; each missing or empty part fails with its `task.*` code; a
  per-task command selector (`accepts:`, `accepts_if:`, `run:`) fails with
  `task.acceptance_run_forbidden`.
- The existing end-to-end and validator suites still pass; migrated task
  files in the vault carry the five-part structure after migration.

## Deferred (agreed)

- A prompt lint that warns when a machine-checkable criterion has "neither
  foot" (no product sentence and no machine command) is a possible follow-up.
