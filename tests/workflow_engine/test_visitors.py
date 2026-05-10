from __future__ import annotations

from pathlib import Path

import pytest

from workflow_engine import (
    parse_yaml,
    load_yaml,
    build_ast,
    validate,
    ValidationVisitor,
    InterpretationVisitor,
    AstVisitor,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _build_document(yaml_src: str):
    parsed = parse_yaml(yaml_src)
    assert parsed.value is not None
    ast_result = build_ast(parsed.value)
    assert ast_result.document is not None
    return ast_result.document


class TestValidationVisitor:
    def test_validation_visitor_used_by_validate(self):
        doc = _build_document("""schema_version: 1
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
        vr = validate(doc)
        assert not vr.valid
        assert any(d.code == "workflow.reference.unknown_state" for d in vr.diagnostics)

    def test_validation_visitor_collects_all_errors(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    task_type: NonexistentTT
    from: NonexistentFrom
    to: NonexistentTo
    worker: NonexistentWorker
""")
        vr = validate(doc)
        assert not vr.valid
        codes = [d.code for d in vr.diagnostics]
        assert "workflow.reference.unknown_task_type" in codes
        assert "workflow.reference.unknown_state" in codes
        assert "workflow.reference.unknown_worker" in codes

    def test_validation_visitor_nested_accept(self):
        doc = _build_document("""schema_version: 1
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
        vr = validate(doc)
        assert not vr.valid
        assert any(d.code == "workflow.reference.unknown_artifact" for d in vr.diagnostics)


class TestInterpretationVisitor:
    def test_interpretation_visitor_returns_data(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        visitor = InterpretationVisitor()
        result = visitor.visit_document(doc)
        assert isinstance(result, dict)
        assert result["type"] == "document"

    def test_interpretation_visitor_state(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        visitor = InterpretationVisitor()
        stmt = doc.statements[0]
        result = stmt.accept(visitor)
        assert isinstance(result, dict)
        assert result["type"] == "state"
        assert result["id"] == "Todo"

    def test_interpretation_visitor_does_not_execute(self):
        doc = _build_document("""schema_version: 1
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
""")
        visitor = InterpretationVisitor()
        result = visitor.visit_document(doc)
        assert isinstance(result, dict)
        assert result["statement_count"] == 3

    def test_interpretation_visitor_transition(self):
        doc = _build_document("""schema_version: 1
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
""")
        visitor = InterpretationVisitor()
        transition = doc.statements[2]
        result = transition.accept(visitor)
        assert isinstance(result, dict)
        assert result["type"] == "transition"
        assert result["id"] == "T"


class TestVisitorProtocol:
    def test_all_statements_accept_visitor(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: S
  - kind: task_type
    id: TT
  - kind: artifact_type
    id: A
  - kind: validation_type
    id: V
  - kind: worker
    id: W
  - kind: operation_type
    id: O
  - kind: transition
    id: T
    task_type: TT
    from: S
    to: S
""")
        visitor = InterpretationVisitor()
        for stmt in doc.statements:
            result = stmt.accept(visitor)
            assert isinstance(result, dict)

    def test_nested_nodes_accept_visitor(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: S
  - kind: task_type
    id: TT
    requirements:
      S:
        artifacts:
          - A
        validations:
          - type: V
  - kind: artifact_type
    id: A
  - kind: validation_type
    id: V
  - kind: transition
    id: T
    task_type: TT
    from: S
    to: S
    requires:
      artifacts:
        - A
      validations:
        - type: V
    transaction:
      steps:
        - op: O
  - kind: operation_type
    id: O
""")
        visitor = InterpretationVisitor()
        for stmt in doc.statements:
            stmt.accept(visitor)

    def test_requirement_set_accept(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: S
  - kind: task_type
    id: TT
    requirements:
      S:
        artifacts:
          - A
  - kind: artifact_type
    id: A
""")
        visitor = InterpretationVisitor()
        tt = doc.statements[1]
        for state_name, req_set in tt.requirements_by_state.items():
            result = req_set.accept(visitor)
            assert isinstance(result, dict)

    def test_transaction_plan_accept(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: S
  - kind: task_type
    id: TT
  - kind: operation_type
    id: O
  - kind: transition
    id: T
    task_type: TT
    from: S
    to: S
    transaction:
      steps:
        - op: O
""")
        visitor = InterpretationVisitor()
        transition = doc.statements[3]
        result = transition.transaction.accept(visitor)
        assert isinstance(result, dict)

    def test_operation_call_accept(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: S
  - kind: task_type
    id: TT
  - kind: operation_type
    id: O
  - kind: transition
    id: T
    task_type: TT
    from: S
    to: S
    transaction:
      steps:
        - op: O
""")
        visitor = InterpretationVisitor()
        transition = doc.statements[3]
        for step in transition.transaction.steps:
            result = step.accept(visitor)
            assert isinstance(result, dict)

    def test_validation_call_accept(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: state
    id: S
  - kind: validation_type
    id: V
  - kind: task_type
    id: TT
    requirements:
      S:
        validations:
          - type: V
""")
        visitor = InterpretationVisitor()
        tt = doc.statements[2]
        for state_name, req_set in tt.requirements_by_state.items():
            for val_call in req_set.validations:
                result = val_call.accept(visitor)
                assert isinstance(result, dict)

    def test_arg_spec_accept(self):
        doc = _build_document("""schema_version: 1
statements:
  - kind: validation_type
    id: V
    args:
      name:
        type: string
""")
        visitor = InterpretationVisitor()
        vt = doc.statements[0]
        for arg_name, arg_spec in vt.args.items():
            result = arg_spec.accept(visitor)
            assert isinstance(result, dict)
