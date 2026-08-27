# Execution Contract Authoring

## Role

Turn the current free-form implementation task into a precise, repository-grounded work order for the implementation model. The user may structure the task in any way. Do not rewrite the task, add contract frontmatter to it, or implement code.

## Required Work

1. Read the task and all linked context.
2. Inspect the repository enough to resolve the actual files, interfaces, toolchain, and focused checks involved.
3. Read `.open-tulid/job-context.json`. Copy `task.id` and the top-level `source_intent_sha256` exactly.
4. Select the narrowest suitable profile and make architecture, interface, scope, failure-behavior, and verification decisions explicit when they matter.
5. Write exactly one artifact: `output/implementation-contract.yaml`.
6. Submit that artifact as `ImplementationContract` and submit evidence for `implementation_contract_valid`.

Do not create generic checks that the repository cannot run. Do not copy workflow or completion instructions into the contract. Do not leave placeholders, open design choices, or prose outside the YAML artifact.

## Contract Schema

```yaml
schema: tulid.implementation/v1
source:
  task_id: "exact-task-id-from-job-context"
  source_intent_sha256: "exact-source-intent-sha256-from-job-context"
profile: code_change
objective: One observable outcome for this task.
change_surface:
  add: []
  edit:
    - path/to/existing-file.py
  forbidden:
    - unrelated/path
interfaces:
  - name: package.module.symbol
    behavior: Exact input, output, state, or compatibility contract.
requirements:
  - A concrete behavior the implementation model must implement.
failure_behavior:
  - An exact error, exit, or fallback behavior when relevant.
non_goals:
  - A tempting adjacent change that is outside this task.
# Include context_excerpts only when one exact section is required:
# context_excerpts:
#   - artifact: ImplementationSpec
#     heading: Exact section heading
#     reason: Why this section is required to implement this contract.
checks:
  focused:
    - id: focused_test
      argv: [python, -m, pytest, tests/path/test_file.py, -q]
      timeout_seconds: 120
      expect:
        exit_code: 0
  invariants: []
  profiles: []
  # When acceptance.yaml requires vertical slices, select one or supply a concrete reason.
  # vertical_slice_exemption: "A deterministic vertical slice is not applicable because ..."
```

Allowed profiles are `bootstrap`, `bug_fix`, `code_change`, `configuration`, `documentation`, `integration`, `refactor`, and `test_only`.

The universal required fields are `schema`, `source`, `profile`, `objective`, `change_surface`, `requirements`, and `checks`. Always quote both source values so numeric task IDs remain strings. The change surface must allow at least one workspace-relative add or edit path. A contract needs at least one focused check, invariant, or acceptance profile. Use argument arrays, never shell control operators. Documentation contracts must include a Markdown path; integration contracts must include at least one invariant. Omit `context_excerpts` unless exact reference text is required; every selection needs one uniquely occurring heading and a concrete reason. When `acceptance.yaml` enables `policy.require_vertical_slice`, product-facing profiles (`bootstrap`, `bug_fix`, `code_change`, `configuration`, `integration`, and `refactor`) must select a project-owned `vertical_slice` acceptance profile in `checks.profiles`, unless `checks.vertical_slice_exemption` records a concrete non-empty reason. Do not declare both.
