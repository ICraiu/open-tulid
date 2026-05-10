from __future__ import annotations

from pathlib import Path

import pytest

from workflow_engine import (
    ValidationResult,
    parse_yaml,
    load_yaml,
    build_ast,
    validate,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _build_and_validate(yaml_src: str):
    parsed = parse_yaml(yaml_src)
    assert parsed.value is not None
    ast_result = build_ast(parsed.value)
    assert ast_result.document is not None
    return validate(ast_result.document)


def _load_and_validate(fixture_name: str):
    result = load_yaml(str(FIXTURES / fixture_name))
    assert result.value is not None
    ast_result = build_ast(result.value)
    assert ast_result.document is not None
    return validate(ast_result.document)


class TestPositiveValidation:
    def test_valid_minimal(self):
        vr = _load_and_validate("valid_minimal.yaml")
        assert isinstance(vr, ValidationResult)
        assert vr.valid is True

    def test_valid_full(self):
        vr = _load_and_validate("valid_full.yaml")
        assert isinstance(vr, ValidationResult)
        assert vr.valid is True

    def test_forward_references_valid(self):
        vr = _load_and_validate("valid_forward_refs.yaml")
        assert vr.valid is True

    def test_reversed_order_valid(self):
        vr = _load_and_validate("valid_reversed_order.yaml")
        assert vr.valid is True

    def test_validate_returns_validation_result(self):
        vr = _load_and_validate("valid_minimal.yaml")
        assert isinstance(vr, ValidationResult)


class TestDuplicateIds:
    def test_duplicate_state_id(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Todo
""")
        assert vr.valid is False
        assert any(d.code == "workflow.symbol.duplicate_id" for d in vr.diagnostics)

    def test_duplicate_task_type_id(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: task_type
    id: T
  - kind: task_type
    id: T
""")
        assert vr.valid is False
        assert any(d.code == "workflow.symbol.duplicate_id" for d in vr.diagnostics)

    def test_same_id_different_kind_allowed(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: Todo
""")
        assert vr.valid is True


class TestTransitionReferences:
    def test_unknown_from_state(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Nonexistent
    to: Todo
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_state" for d in vr.diagnostics)

    def test_unknown_to_state(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_state" for d in vr.diagnostics)

    def test_unknown_task_type(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: transition
    id: T
    task_type: Nonexistent
    from: Todo
    to: Todo
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_task_type" for d in vr.diagnostics)

    def test_unknown_worker(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    worker: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_worker" for d in vr.diagnostics)


class TestTaskTypeRequirements:
    def test_unknown_state_in_requirements(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
    requirements:
      Nonexistent:
        artifacts: []
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_state" for d in vr.diagnostics)

    def test_unknown_artifact_in_requirements(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
    requirements:
      Todo:
        artifacts:
          - Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_artifact" for d in vr.diagnostics)

    def test_unknown_validation_in_requirements(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
    requirements:
      Todo:
        validations:
          - type: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_validation" for d in vr.diagnostics)


class TestTransitionRequirements:
    def test_unknown_artifact_in_transition_requires(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      artifacts:
        - Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_artifact" for d in vr.diagnostics)

    def test_unknown_validation_in_transition_requires(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_validation" for d in vr.diagnostics)


class TestTransactionOperations:
    def test_unknown_operation(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - op: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_operation" for d in vr.diagnostics)


class TestValidationCallArgs:
    def test_missing_required_validation_arg(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      name:
        type: string
        required: true
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: check
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.missing_required_argument" for d in vr.diagnostics)

    def test_unknown_validation_arg(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      name:
        type: string
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: check
          args:
            unknown_arg: value
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.unknown_argument" for d in vr.diagnostics)


class TestOperationCallArgs:
    def test_missing_required_operation_arg(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: operation_type
    id: move
    args:
      to:
        type: state_ref
        required: true
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - op: move
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.missing_required_argument" for d in vr.diagnostics)

    def test_unknown_operation_arg(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: operation_type
    id: move
    args:
      to:
        type: state_ref
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - op: move
          args:
            unknown_arg: value
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.unknown_argument" for d in vr.diagnostics)


class TestArgTypes:
    def test_scalar_arg_wrong_type_string(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      name:
        type: string
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: check
          args:
            name: 123
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.wrong_argument_type" for d in vr.diagnostics)

    def test_scalar_arg_wrong_type_integer(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      count:
        type: integer
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: check
          args:
            count: not-an-int
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.wrong_argument_type" for d in vr.diagnostics)

    def test_scalar_arg_wrong_type_boolean(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      flag:
        type: boolean
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: check
          args:
            flag: not-a-bool
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.wrong_argument_type" for d in vr.diagnostics)

    def test_many_true_receives_scalar(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: operation_type
    id: op
    args:
      items:
        type: string
        many: true
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - op: op
          args:
            items: single
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.wrong_argument_type" for d in vr.diagnostics)

    def test_many_false_receives_list(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: operation_type
    id: op
    args:
      item:
        type: string
        many: false
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - op: op
          args:
            item:
              - a
              - b
""")
        assert vr.valid is False
        assert any(d.code == "workflow.call.wrong_argument_type" for d in vr.diagnostics)


class TestReferenceArgs:
    def test_artifact_ref_unknown(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      artifact:
        type: artifact_ref
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: check
          args:
            artifact: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_artifact" for d in vr.diagnostics)

    def test_state_ref_unknown(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: operation_type
    id: move
    args:
      to:
        type: state_ref
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - op: move
          args:
            to: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_state" for d in vr.diagnostics)

    def test_operation_ref_unknown(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: operation_type
    id: chain
    args:
      next_op:
        type: operation_ref
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - op: chain
          args:
            next_op: Nonexistent
""")
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_operation" for d in vr.diagnostics)


class TestNoCrashProbes:
    def test_validation_not_map_no_crash(self):
        result = load_yaml(str(FIXTURES / "nocrash_validation_not_map.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is None
        assert any(d.code == "workflow.shape.wrong_type" for d in ast_result.diagnostics)

    def test_op_not_map_no_crash(self):
        result = load_yaml(str(FIXTURES / "nocrash_op_not_map.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is None
        assert any(d.code == "workflow.shape.wrong_type" for d in ast_result.diagnostics)

    def test_arg_many_not_bool_no_crash(self):
        result = load_yaml(str(FIXTURES / "nocrash_arg_many_not_bool.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is None
        assert any(d.code == "workflow.shape.wrong_type" for d in ast_result.diagnostics)

    def test_missing_task_type_no_crash(self):
        result = load_yaml(str(FIXTURES / "nocrash_missing_task_type.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is None
        assert any(d.code == "workflow.shape.missing_required_field" for d in ast_result.diagnostics)

    def test_unknown_artifact_ref_no_crash(self):
        result = load_yaml(str(FIXTURES / "nocrash_unknown_artifact_ref.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is not None
        vr = validate(ast_result.document)
        assert vr.valid is False
        assert any(d.code == "workflow.reference.unknown_artifact" for d in vr.diagnostics)

    def test_wrong_arg_type_no_crash(self):
        result = load_yaml(str(FIXTURES / "nocrash_wrong_arg_type.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is not None
        vr = validate(ast_result.document)
        assert vr.valid is False
        assert any(d.code == "workflow.call.wrong_argument_type" for d in vr.diagnostics)


class TestSemanticDiagnostics:
    def test_semantic_diagnostic_has_path(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Nonexistent
    to: Todo
""")
        for d in vr.diagnostics:
            if d.code == "workflow.reference.unknown_state":
                assert d.path is not None

    def test_semantic_diagnostic_has_line_column(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Nonexistent
    to: Todo
""")
        for d in vr.diagnostics:
            if d.code == "workflow.reference.unknown_state":
                assert d.line is not None
                assert d.line >= 1

    def test_no_question_mark_in_semantic_paths(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Nonexistent
    to: Todo
""")
        for d in vr.diagnostics:
            if d.path:
                assert "?" not in d.path


class TestSemanticFieldPaths:
    """Tests for spec-compliant field-level semantic diagnostic paths."""

    def test_unknown_to_at_index_3_has_correct_path(self):
        """Unknown transition `to` at statement index 3 reports path == 'statements[3].to'."""
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: state
    id: S2
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: S1
    to: Nonexistent
""")
        to_diags = [d for d in vr.diagnostics
                    if d.code == "workflow.reference.unknown_state"
                    and "to" in (d.message or "")]
        assert len(to_diags) >= 1
        d = to_diags[0]
        assert d.path == "statements[3].to", f"expected statements[3].to, got {d.path}"
        assert d.line is not None and d.line >= 1
        assert d.column is not None and d.column >= 1

    def test_unknown_from_has_correct_path(self):
        """Unknown transition `from` reports field-specific path."""
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: Nonexistent
    to: S1
""")
        from_diags = [d for d in vr.diagnostics
                      if d.code == "workflow.reference.unknown_state"
                      and "from" in (d.message or "")]
        assert len(from_diags) >= 1
        d = from_diags[0]
        assert d.path == "statements[2].from", f"expected statements[2].from, got {d.path}"

    def test_unknown_task_type_has_correct_path(self):
        """Unknown task_type reports field-specific path."""
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: transition
    id: T
    task_type: Nonexistent
    from: S1
    to: S1
""")
        tt_diags = [d for d in vr.diagnostics
                    if d.code == "workflow.reference.unknown_task_type"]
        assert len(tt_diags) >= 1
        d = tt_diags[0]
        assert d.path == "statements[1].task_type", f"expected statements[1].task_type, got {d.path}"

    def test_unknown_worker_has_correct_path(self):
        """Unknown worker reports field-specific path."""
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: S1
    to: S1
    worker: Nonexistent
""")
        w_diags = [d for d in vr.diagnostics
                   if d.code == "workflow.reference.unknown_worker"]
        assert len(w_diags) >= 1
        d = w_diags[0]
        assert d.path == "statements[2].worker", f"expected statements[2].worker, got {d.path}"

    def test_unknown_artifact_in_requires_has_exact_item_path(self):
        """Unknown artifact in requires.artifacts reports the exact list item path."""
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: TT
    from: S1
    to: S1
    requires:
      artifacts:
        - Nonexistent
""")
        art_diags = [d for d in vr.diagnostics
                     if d.code == "workflow.reference.unknown_artifact"]
        assert len(art_diags) >= 1
        d = art_diags[0]
        assert d.path == "statements[2].requires.artifacts[0]"
        assert d.line is not None and d.line >= 1
        assert d.column is not None and d.column >= 1

    def test_wrong_arg_type_in_validation_call_has_exact_arg_path(self):
        """Wrong arg type in validation call reports the exact arg path."""
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      count:
        type: integer
        required: true
  - kind: transition
    id: T
    task_type: TT
    from: S1
    to: S1
    requires:
      validations:
        - type: check
          args:
            count: not-an-int
""")
        wrong = [d for d in vr.diagnostics
                 if d.code == "workflow.call.wrong_argument_type"]
        assert len(wrong) >= 1
        d = wrong[0]
        assert d.path == "statements[3].requires.validations[0].args.count"
        assert d.line is not None and d.line >= 1

    def test_unknown_validation_arg_has_exact_arg_path(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      count:
        type: integer
  - kind: transition
    id: T
    task_type: TT
    from: S1
    to: S1
    requires:
      validations:
        - type: check
          args:
            extra: 1
""")
        diags = [d for d in vr.diagnostics if d.code == "workflow.call.unknown_argument"]
        assert len(diags) >= 1
        d = diags[0]
        assert d.path == "statements[3].requires.validations[0].args.extra"
        assert d.line is not None and d.line >= 1

    def test_missing_validation_arg_has_synthetic_exact_arg_path(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: task_type
    id: TT
  - kind: validation_type
    id: check
    args:
      count:
        type: integer
        required: true
  - kind: transition
    id: T
    task_type: TT
    from: S1
    to: S1
    requires:
      validations:
        - type: check
          args: {}
""")
        diags = [d for d in vr.diagnostics if d.code == "workflow.call.missing_required_argument"]
        assert len(diags) >= 1
        d = diags[0]
        assert d.path == "statements[3].requires.validations[0].args.count"
        assert d.line is not None and d.line >= 1

    def test_operation_arg_reference_has_exact_arg_path(self):
        vr = _build_and_validate("""schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: task_type
    id: TT
  - kind: operation_type
    id: move
    args:
      to:
        type: state_ref
  - kind: transition
    id: T
    task_type: TT
    from: S1
    to: S1
    transaction:
      steps:
        - op: move
          args:
            to: Missing
""")
        diags = [d for d in vr.diagnostics if d.code == "workflow.reference.unknown_state"]
        assert len(diags) >= 1
        d = diags[0]
        assert d.path == "statements[3].transaction.steps[0].args.to"
        assert d.line is not None and d.line >= 1
