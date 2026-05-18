# Workflow Definition Compiler Review And Fix Brief

Audience: Qwen 27B working in this repository.

Goal: finish the implementation of the workflow definition compiler slice from `spec/34-workflow-definition-compiler.md` without broad refactors or runtime execution work.

Current status: the implementation is mostly complete, but not production ready. The workflow test slice currently passes, but several contract requirements are incomplete or under-tested. Treat this file as a focused repair brief.

## Important Boundaries

Do not turn this into a workflow runtime. This slice only compiles a validated `workflow_engine.WorkflowDocument` into an `open_tulid.workflow.WorkflowDefinition`.

Do not add CLI commands.

Do not read or write vault files.

Do not run git, subprocesses, workers, validators, operations, cleanup, notifications, or kanban updates.

Do not import `open_tulid.cli`, `open_tulid.vault`, or `open_tulid.domain` from `src/open_tulid/workflow/*`.

Do not import `open_tulid` from `src/workflow_engine/*`.

Prefer small local changes in:

- `src/open_tulid/workflow/definitions.py`
- `src/open_tulid/workflow/compiler.py`
- `src/open_tulid/workflow/registry.py`
- `tests/workflow/test_compiler.py`
- `tests/workflow/test_registry.py`
- `tests/workflow/test_boundaries.py` only if needed

Avoid unrelated formatting churn.

## Spec Coverage Assessment

Estimated implementation coverage before fixes: about 85%.

Implemented:

- Required `open_tulid.workflow` package exists.
- Public API is exported from `open_tulid.workflow.__init__`.
- Built-in validation, operation, worker, artifact handler, and template handler registries exist.
- Registry entries are frozen dataclasses.
- Registry builder detects duplicate IDs, empty IDs, missing implementations, invalid argument types, and unknown cleanup operations.
- Built-in `git_reset_hard` is marked destructive and approval gated.
- Compiler uses built-in registries by default.
- Compiler validates registry integrity before compiling.
- Compiler converts current AST statement types into workflow definition nodes.
- Compiler rejects unsupported validation, operation, and worker implementations.
- Compiler returns diagnostics instead of raising for normal compile failures.
- Compiler returns no definition when errors exist.
- Boundary tests verify the main import and no-execution constraints.

Incomplete:

- `ArtifactTypeDefinition.handler` is missing.
- Artifact handler compile checks are not implemented.
- Template handler compile checks are not implemented.
- Required diagnostic codes for artifact/template support are not emitted.
- Workflow definitions are only shallowly immutable.
- Some compile diagnostics lose available AST span/path information.
- `validate_registries()` can miss duplicate logical IDs and key/spec mismatches when a `RuntimeRegistries` object is manually constructed.
- Tests do not cover the above gaps.

## Fix 1: Add `ArtifactTypeDefinition.handler`

Spec requires:

```python
@dataclass(frozen=True)
class ArtifactTypeDefinition:
    id: str
    template: str | None = None
    handler: str | None = None
```

Current file: `src/open_tulid/workflow/definitions.py`

Add the `handler` field with default `None`.

Current DSL does not have an artifact handler or medium field. Therefore, in `compile_workflow()`, when compiling an `ArtifactTypeStatement`, set:

```python
ArtifactTypeDefinition(
    id=stmt.id,
    template=stmt.template,
    handler=None,
)
```

Do not reject handlerless artifacts. The spec explicitly says handler support is limited in this slice and handlerless artifact types are allowed.

Add a test that compiles an artifact type and verifies:

- `artifact.template` is preserved.
- `artifact.handler is None`.

## Fix 2: Implement Artifact Handler Diagnostic Surface Conservatively

Required diagnostic code:

```text
workflow.compile.unsupported_artifact_handler
```

The current DSL has no `handler` or `medium` field on `artifact_type`, so there is no normal YAML syntax that can trigger this diagnostic yet.

Do not invent new DSL syntax just to trigger this.

However, make the compiler structurally ready:

- Add a helper that can validate an artifact handler ID against `registries.artifact_handlers`.
- The helper should emit `workflow.compile.unsupported_artifact_handler` if a non-`None` handler is unsupported.
- Wire it so it is used if/when an artifact handler value is available.
- For current AST, handler should always be `None`, so the helper should not emit anything.

If you add no helper because there is literally no AST field to call it with, add a clear comment near artifact compilation explaining that the diagnostic code becomes reachable when the DSL adds a handler field. But prefer a small helper because it reduces future ambiguity.

Do not add a fake handler attribute to `workflow_engine.ArtifactTypeStatement` unless the spec for the DSL has also been changed. This task is only for the compiler slice.

Testing:

- Do not write brittle tests that monkeypatch arbitrary attributes onto a frozen AST dataclass.
- It is acceptable to test this indirectly once DSL support exists.
- For now, tests should verify handlerless artifact compilation succeeds.

## Fix 3: Implement Template Handler Diagnostic Surface Conservatively

Required diagnostic code:

```text
workflow.compile.unsupported_template
```

Current DSL has:

```yaml
artifact_type:
  id: X
  template: optional-string
```

The spec says:

- `artifact_type.template`, if present, is treated as a template reference string only.
- Do reject template references when template handler support is explicitly required by a future DSL field.

This means a plain string like `templates/summary.md` must not be rejected merely because it is not a template handler ID.

Do not incorrectly validate `artifact_type.template` against `registries.template_handlers`. That would break current DSL compatibility.

Instead:

- Keep preserving `template` as a string.
- Add the unsupported template diagnostic code only in a helper or future-ready branch that is used when a future AST field explicitly identifies a template handler.
- Document clearly in code that current `template` is a reference, not a handler ID.

Testing:

- Add a test proving `template: templates/summary.md` still compiles.
- Avoid adding tests that expect `templates/summary.md` to exist on disk. This compiler must not read files.

## Fix 4: Make Runtime Definitions Deeply Immutable Enough For Production

Problem: dataclasses are frozen, but their mapping fields currently receive mutable dicts from the compiler. A caller can still mutate:

```python
result.definition.states["New"] = StateDefinition(id="New")
```

That violates the spirit of immutable runtime definitions.

Recommended fix:

- Use `types.MappingProxyType` for all mapping fields in compiled definitions.
- Add a small local helper in `compiler.py`, for example:

```python
from types import MappingProxyType

def _freeze_mapping(mapping: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType(dict(mapping))
```

Since Python typing around generic helper functions can be awkward, keep it simple. Avoid over-engineering.

Freeze at least these top-level maps when constructing `WorkflowDefinition`:

- `states`
- `task_types`
- `artifact_types`
- `validation_types`
- `operation_types`
- `workers`
- `transitions`

Also freeze nested maps:

- `TaskTypeDefinition.requirements_by_state`
- `ValidationTypeDefinition.args`
- `OperationTypeDefinition.args`
- `ValidationCallDefinition.args`
- `OperationCallDefinition.args`

Registry containers are also frozen dataclasses with mutable mapping fields. Consider freezing maps returned by `build_registries()` too:

- `validations`
- `operations`
- `workers`
- `artifact_handlers`
- `template_handlers`

Do not mutate public APIs to return plain tuples instead of mappings. Keep the declared API shape as `Mapping[...]`.

Tests to add:

- Top-level definition maps cannot be mutated.
- Nested `requirements_by_state` cannot be mutated.
- Nested validation/operation args cannot be mutated.
- Registry maps returned from `build_registries()` cannot be mutated, if you implement registry map freezing.

Use `pytest.raises(TypeError)` or a broad exception only if needed. Prefer the precise expected exception if stable.

## Fix 5: Preserve AST Span Information For Cross-Reference Diagnostics

Unsupported validation/operation/worker diagnostics already preserve statement spans.

Cross-reference diagnostics currently lose spans because `_validate_cross_references()` receives only normalized definition nodes, not the original AST statements. Examples:

- unknown state in task type requirements
- unknown artifact in requirements
- unknown validation call in requirements
- unknown task type in transition
- unknown from/to state in transition
- unknown worker in transition
- unknown operation in transaction step

The AST already carries useful spans:

- `TaskTypeStatement.span`
- `RequirementSet.span`
- `RequirementSet.artifact_spans`
- `ValidationCall.span`
- `TransitionStatement.span`
- `TransitionStatement.field_spans`
- `OperationCall.span`
- `TransactionPlan.span`

Recommended approach:

1. Keep definition conversion separate from diagnostic generation.
2. During the main statement loop in `compile_workflow()`, validate cross references while the original statement is still available, or store a small statement lookup by ID.
3. Use the most specific span available:
   - task type requirement state key: `RequirementSet.span` or task statement span
   - requirement artifact item: matching entry from `RequirementSet.artifact_spans`
   - validation call: `ValidationCall.span`
   - transition `task_type`: `TransitionStatement.field_spans["task_type"]` or transition span
   - transition `from`: `field_spans["from"]` or transition span
   - transition `to`: `field_spans["to"]` or transition span
   - transition `worker`: `field_spans["worker"]` or transition span
   - transaction operation step: `OperationCall.span`

Do not duplicate all `workflow_engine` semantic validation. The compiler should only check runtime registry and installed-runtime availability.

Tests to add:

- Unknown transition operation includes non-`None` path/line/column when source was loaded from YAML.
- Unknown transition worker includes a field path ending in `.worker` or at least includes the transition path.
- Unknown requirement artifact includes a path pointing at the artifact item or requirement.

Use existing loader/build helpers in `tests/workflow/test_compiler.py` so spans are realistic.

## Fix 6: Strengthen `validate_registries()`

Problem: `build_registries()` catches duplicate IDs because it receives iterables. But callers can manually construct:

```python
RuntimeRegistries(
    validations={
        "a": ValidationSpec(id="same", implementation=object()),
        "b": ValidationSpec(id="same", implementation=object()),
    },
    ...
)
```

`validate_registries()` should detect this as duplicate logical IDs.

Also detect key/spec mismatch:

```python
validations={"map_key": ValidationSpec(id="different", implementation=object())}
```

The registry ID is part of the public contract. A mapping key mismatch will create ambiguous behavior.

Recommended diagnostics:

- Duplicate logical IDs: `workflow.compile.registry_duplicate_id`
- Empty IDs: `workflow.compile.registry_duplicate_id` is already used by the existing implementation, even though the name is imperfect.
- Key/spec ID mismatch: use `workflow.compile.registry_duplicate_id` or `workflow.compile.registry_missing_implementation` only if you must stay within existing required codes. Better: add a precise diagnostic only if the surrounding tests and spec allow non-required codes. If uncertain, use `workflow.compile.registry_duplicate_id` with a clear message.

Tests to add:

- `validate_registries()` rejects duplicate `ValidationSpec.id` values under different mapping keys.
- Same for `OperationSpec.id`.
- Same for worker/artifact/template specs if you can do it without excessive duplication.
- `validate_registries()` rejects a mapping key that differs from `spec.id`.

Keep the implementation simple. A helper that iterates `(key, spec)` pairs and tracks `spec.id` is enough.

## Fix 7: Required Diagnostic Codes Should Exist In Tests

The spec lists these required diagnostic codes:

```text
workflow.compile.unsupported_artifact_handler
workflow.compile.unsupported_validation
workflow.compile.unsupported_operation
workflow.compile.unsupported_worker
workflow.compile.unsupported_template
workflow.compile.registry_duplicate_id
workflow.compile.registry_invalid_argument_type
workflow.compile.registry_missing_implementation
```

Current tests cover most but not all.

Add tests or explicit comments for currently unreachable DSL-driven diagnostics:

- `workflow.compile.unsupported_artifact_handler`
- `workflow.compile.unsupported_template`

Because current DSL lacks fields to trigger these normally, do not force false behavior. Prefer documenting the limitation and proving current behavior remains compatible.

If you choose to add internal helper tests for these diagnostics, keep helpers private only if you are comfortable testing through a narrow public scenario. Do not export helper functions just for tests unless there is a good public API reason.

## Fix 8: Reconsider Over-Eager Cross-Reference Checks

The spec says:

> Preserve DSL validation semantics already guaranteed by `workflow_engine`; do not duplicate all DSL semantic validation.

The current compiler performs cross-reference checks for states, task types, artifacts, validations, workers, and operations. Some of this may duplicate `workflow_engine`.

Do not remove these checks blindly. They are useful for compile-time safety. But make sure they are not inconsistent with `workflow_engine` diagnostics or weaker on spans.

If a check is already guaranteed by `workflow_engine`, it is acceptable for the compiler to rely on that guarantee, provided tests and behavior remain aligned. If keeping duplicate checks, make them high quality and span-aware.

## Production Readiness Bar

After fixes, this slice can be considered production ready only if:

- All tests pass.
- The compiler cannot mutate files, call subprocess, or import runtime adapters.
- All public definitions returned by `compile_workflow()` are effectively immutable to callers.
- Registry validation catches invalid registries whether built via `build_registries()` or manually constructed.
- Diagnostics are actionable and preserve source locations where the AST provides them.
- Current DSL compatibility is preserved: plain `artifact_type.template` strings compile as references and are not treated as template handler IDs.
- Built-in registries remain placeholders and do not execute.

## Verification Commands

Run:

```bash
uv run pytest -q tests/workflow
```

Then run the broader suite if time allows:

```bash
uv run pytest -q
```

Before finalizing, check:

```bash
git diff -- src/open_tulid/workflow tests/workflow
```

Review the diff for accidental runtime behavior, imports, or unrelated formatting.

## Expected Final Report

When done, report:

- Files changed.
- Which missing spec items were fixed.
- Which artifact/template diagnostics remain intentionally unreachable from current DSL syntax.
- Test commands run and results.
- Any remaining production-readiness concerns.

