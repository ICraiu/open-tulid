from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import workflow_engine
from workflow_engine import (
    AstBuildResult,
    ParseResult,
    parse_yaml,
    load_yaml,
    build_ast,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseYaml:
    def test_parse_valid_minimal(self):
        result = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n")
        assert isinstance(result, ParseResult)
        assert result.diagnostics == ()
        assert result.value is not None

    def test_parse_yaml_returns_parse_result(self):
        result = parse_yaml("schema_version: 1\nstatements: []\n")
        assert isinstance(result, ParseResult)

    def test_malformed_yaml(self):
        result = parse_yaml(": : :\n  - [")
        assert isinstance(result, ParseResult)
        assert len(result.diagnostics) > 0
        assert result.diagnostics[0].code == "workflow.yaml.parse_error"
        assert result.value is None

    def test_malformed_yaml_uses_source_name(self):
        result = parse_yaml(": : :\n  - [", source_name="myworkflow.yaml")
        assert result.diagnostics[0].path == "myworkflow.yaml"

    def test_root_is_list(self):
        result = parse_yaml("- item1\n- item2\n")
        assert isinstance(result, ParseResult)
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.diagnostics[0].code == "workflow.shape.root_not_mapping"

    def test_missing_schema_version(self):
        parsed = parse_yaml("statements: []\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_schema_version_wrong_type_string(self):
        parsed = parse_yaml("schema_version: '1'\nstatements: []\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_schema_version_wrong_type_bool(self):
        parsed = parse_yaml("schema_version: true\nstatements: []\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_unsupported_schema_version(self):
        parsed = parse_yaml("schema_version: 2\nstatements: []\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.schema.unsupported_version" for d in result.diagnostics)

    def test_missing_statements(self):
        parsed = parse_yaml("schema_version: 1\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_statements_not_list(self):
        parsed = parse_yaml("schema_version: 1\nstatements: {}\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_statement_not_mapping(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - 'not-a-map'\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_missing_kind(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - id: Todo\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_non_string_kind(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: 123\n    id: Todo\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_unknown_kind(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state_type\n    id: Todo\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.statement.unknown_kind" for d in result.diagnostics)

    def test_missing_id(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_non_string_id(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: 123\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_unknown_top_level_key(self):
        parsed = parse_yaml("schema_version: 1\nstatements: []\nextra: true\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.unknown_key" for d in result.diagnostics)

    def test_unknown_statement_key(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n    extra: true\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.unknown_key" for d in result.diagnostics)

    def test_artifact_template_wrong_type(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: artifact_type\n    id: A\n    template: 123\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_worker_type_wrong_type(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: worker\n    id: W\n    type: 123\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_validation_call_not_mapping(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        validations:\n          - not-a-map\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)
        assert result.document is None

    def test_validation_call_missing_type(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        validations:\n          - args: {}\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_validation_call_unknown_key(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        validations:\n          - type: v\n            extra: true\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.unknown_key" for d in result.diagnostics)

    def test_validation_call_args_not_mapping(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        validations:\n          - type: v\n            args: [1,2]\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_operation_step_not_mapping(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n    to: S\n    transaction:\n      steps:\n        - not-a-map\n  - kind: task_type\n    id: TT\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)
        assert result.document is None

    def test_operation_call_missing_op(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n    to: S\n    transaction:\n      steps:\n        - args: {}\n  - kind: task_type\n    id: TT\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_operation_call_unknown_key(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n    to: S\n    transaction:\n      steps:\n        - op: o\n          extra: true\n  - kind: task_type\n    id: TT\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.unknown_key" for d in result.diagnostics)

    def test_operation_call_args_not_mapping(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n    to: S\n    transaction:\n      steps:\n        - op: o\n          args: [1,2]\n  - kind: task_type\n    id: TT\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_arg_spec_not_mapping(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: validation_type\n    id: v\n    args:\n      x: 'not-a-map'\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_arg_spec_missing_type(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: validation_type\n    id: v\n    args:\n      x:\n        required: true\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_arg_spec_unknown_type(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: validation_type\n    id: v\n    args:\n      x:\n        type: unknown_type\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.call.unknown_argument_type" for d in result.diagnostics)

    def test_arg_spec_required_not_boolean(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: validation_type\n    id: v\n    args:\n      x:\n        type: string\n        required: yes\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)

    def test_arg_spec_many_not_boolean(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: operation_type\n    id: bad\n    args:\n      x:\n        type: string\n        many: nope\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)
        assert result.document is None

    def test_transition_missing_task_type(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n  - kind: transition\n    id: T\n    from: Todo\n    to: Done\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)
        assert result.document is None

    def test_transition_missing_from(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n  - kind: task_type\n    id: TT\n  - kind: transition\n    id: T\n    task_type: TT\n    to: Todo\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)
        assert result.document is None

    def test_transition_missing_to(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n  - kind: task_type\n    id: TT\n  - kind: transition\n    id: T\n    task_type: TT\n    from: Todo\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)
        assert result.document is None

    def test_artifact_item_not_string(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        artifacts:\n          - 123\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert any(d.code == "workflow.shape.wrong_type" for d in result.diagnostics)


class TestLoadYaml:
    def test_load_valid_minimal(self):
        result = load_yaml(str(FIXTURES / "valid_minimal.yaml"))
        assert isinstance(result, ParseResult)
        assert result.diagnostics == ()
        assert result.value is not None

    def test_load_valid_full(self):
        result = load_yaml(str(FIXTURES / "valid_full.yaml"))
        assert isinstance(result, ParseResult)
        assert result.diagnostics == ()
        assert result.value is not None

    def test_load_nonexistent_file(self):
        result = load_yaml("/nonexistent/path/file.yaml")
        assert isinstance(result, ParseResult)
        assert len(result.diagnostics) > 0
        assert result.diagnostics[0].code == "workflow.yaml.parse_error"


class TestBuildAst:
    def test_build_ast_returns_ast_build_result(self):
        parsed = parse_yaml("schema_version: 1\nstatements: []\n")
        result = build_ast(parsed.value)
        assert isinstance(result, AstBuildResult)

    def test_build_ast_none_parsed(self):
        result = build_ast(None)
        assert isinstance(result, AstBuildResult)
        assert result.document is None
        assert len(result.diagnostics) > 0

    def test_valid_minimal_builds_document(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n")
        result = build_ast(parsed.value)
        assert result.document is not None
        assert result.diagnostics == ()
        assert len(result.document.statements) == 1

    def test_valid_full_builds_document(self):
        result = load_yaml(str(FIXTURES / "valid_full.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is not None
        assert ast_result.diagnostics == ()

    def test_comments_accepted(self):
        result = load_yaml(str(FIXTURES / "valid_comments.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is not None
        assert ast_result.diagnostics == ()

    def test_forward_references_accepted(self):
        result = load_yaml(str(FIXTURES / "valid_forward_refs.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is not None
        assert ast_result.diagnostics == ()

    def test_reversed_order_accepted(self):
        result = load_yaml(str(FIXTURES / "valid_reversed_order.yaml"))
        assert result.value is not None
        ast_result = build_ast(result.value)
        assert ast_result.document is not None
        assert ast_result.diagnostics == ()

    def test_ast_nodes_are_immutable(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n")
        result = build_ast(parsed.value)
        assert result.document is not None
        stmt = result.document.statements[0]
        with pytest.raises(Exception):
            stmt.id = "Hacked"

    def test_empty_statements_allowed(self):
        parsed = parse_yaml("schema_version: 1\nstatements: []\n")
        result = build_ast(parsed.value)
        assert result.document is not None
        assert result.document.statements == ()

    def test_no_partial_transition_on_missing_required(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n  - kind: transition\n    id: T\n    from: Todo\n    to: Done\n")
        result = build_ast(parsed.value)
        assert result.document is None
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)

    def test_no_partial_transition_on_missing_task_type(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n  - kind: transition\n    id: T\n    from: Todo\n    to: Done\n")
        result = build_ast(parsed.value)
        assert result.document is None
        assert any(d.code == "workflow.shape.missing_required_field" for d in result.diagnostics)
        assert any("task_type" in (d.path or "") for d in result.diagnostics if d.code == "workflow.shape.missing_required_field")

    def test_shape_error_prevents_document(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state_type\n    id: Todo\n")
        result = build_ast(parsed.value)
        assert result.document is None


class TestDiagnostics:
    def test_diagnostic_has_code_message_severity(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state_type\n    id: Todo\n")
        result = build_ast(parsed.value)
        for d in result.diagnostics:
            assert d.code
            assert d.message
            assert d.severity in ("error", "warning")

    def test_shape_diagnostic_has_path(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n    extra: true\n")
        result = build_ast(parsed.value)
        for d in result.diagnostics:
            if d.code == "workflow.shape.unknown_key":
                assert d.path is not None

    def test_statement_level_diagnostic_has_line_column(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state_type\n    id: Todo\n")
        result = build_ast(parsed.value)
        for d in result.diagnostics:
            if d.code == "workflow.statement.unknown_kind":
                assert d.line is not None
                assert d.line >= 1
                assert d.column is not None
                assert d.column >= 1

    def test_nested_diagnostic_has_line_column(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n    extra: true\n")
        result = build_ast(parsed.value)
        for d in result.diagnostics:
            if d.code == "workflow.shape.unknown_key":
                assert d.line is not None
                assert d.line >= 1

    def test_no_question_mark_in_paths(self):
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n    extra: true\n")
        result = build_ast(parsed.value)
        for d in result.diagnostics:
            if d.path:
                assert "?" not in d.path

    def test_transition_at_index_3_not_report_as_index_0(self):
        yaml_src = """schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: state
    id: S2
  - kind: task_type
    id: TT
  - kind: transition
    id: T
    from: S1
    to: S2
"""
        parsed = parse_yaml(yaml_src)
        result = build_ast(parsed.value)
        for d in result.diagnostics:
            if d.path and "statements[0]" in d.path:
                assert d.code != "workflow.shape.missing_required_field"


class TestFieldLevelDiagnostics:
    """Tests for spec-compliant field-level diagnostic paths and line/column."""

    def test_unknown_transition_to_reports_field_path(self):
        """Unknown transition `to` at index 3 reports path == 'statements[3].to'."""
        yaml_src = """schema_version: 1
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
"""
        parsed = parse_yaml(yaml_src)
        assert parsed.value is not None
        ast_result = build_ast(parsed.value)
        assert ast_result.document is not None
        from workflow_engine import validate
        vr = validate(ast_result.document)
        to_diags = [d for d in vr.diagnostics if d.code == "workflow.reference.unknown_state"
                    and "to" in (d.message or "")]
        assert len(to_diags) >= 1
        d = to_diags[0]
        assert d.path is not None
        assert d.path == "statements[3].to", f"expected statements[3].to, got {d.path}"
        assert d.line is not None and d.line >= 1
        assert d.column is not None and d.column >= 1

    def test_unknown_transition_from_reports_field_path(self):
        """Unknown transition `from` reports path == 'statements[2].from'."""
        yaml_src = """schema_version: 1
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
"""
        parsed = parse_yaml(yaml_src)
        assert parsed.value is not None
        ast_result = build_ast(parsed.value)
        assert ast_result.document is not None
        from workflow_engine import validate
        vr = validate(ast_result.document)
        from_diags = [d for d in vr.diagnostics if d.code == "workflow.reference.unknown_state"
                      and "from" in (d.message or "")]
        assert len(from_diags) >= 1
        d = from_diags[0]
        assert d.path is not None
        assert d.path == "statements[2].from", f"expected statements[2].from, got {d.path}"

    def test_unknown_task_type_reports_field_path(self):
        """Unknown task_type reports path == 'statements[2].task_type'."""
        yaml_src = """schema_version: 1
statements:
  - kind: state
    id: S1
  - kind: transition
    id: T
    task_type: Nonexistent
    from: S1
    to: S1
"""
        parsed = parse_yaml(yaml_src)
        assert parsed.value is not None
        ast_result = build_ast(parsed.value)
        assert ast_result.document is not None
        from workflow_engine import validate
        vr = validate(ast_result.document)
        tt_diags = [d for d in vr.diagnostics if d.code == "workflow.reference.unknown_task_type"]
        assert len(tt_diags) >= 1
        d = tt_diags[0]
        assert d.path is not None
        assert d.path == "statements[1].task_type", f"expected statements[1].task_type, got {d.path}"

    def test_unknown_worker_reports_field_path(self):
        """Unknown worker reports path == 'statements[2].worker'."""
        yaml_src = """schema_version: 1
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
"""
        parsed = parse_yaml(yaml_src)
        assert parsed.value is not None
        ast_result = build_ast(parsed.value)
        assert ast_result.document is not None
        from workflow_engine import validate
        vr = validate(ast_result.document)
        w_diags = [d for d in vr.diagnostics if d.code == "workflow.reference.unknown_worker"]
        assert len(w_diags) >= 1
        d = w_diags[0]
        assert d.path is not None
        assert d.path == "statements[2].worker", f"expected statements[2].worker, got {d.path}"

    def test_many_nope_has_line_column(self):
        """many: nope has non-None one-based line/column."""
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: operation_type\n    id: bad\n    args:\n      x:\n        type: string\n        many: nope\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert result.document is None
        wrong_type = [d for d in result.diagnostics if d.code == "workflow.shape.wrong_type"]
        assert len(wrong_type) >= 1
        d = wrong_type[0]
        assert d.line is not None and d.line >= 1, f"line should be populated, got {d.line}"
        assert d.column is not None and d.column >= 1, f"column should be populated, got {d.column}"
        assert "many" in (d.path or ""), f"path should mention many, got {d.path}"

    def test_validation_call_not_mapping_has_line_column(self):
        """Validation call not mapping has non-None line/column."""
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        validations:\n          - not-a-map\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert result.document is None
        wrong_type = [d for d in result.diagnostics if d.code == "workflow.shape.wrong_type"]
        assert len(wrong_type) >= 1
        d = wrong_type[0]
        assert d.line is not None and d.line >= 1, f"line should be populated, got {d.line}"
        assert d.column is not None and d.column >= 1, f"column should be populated, got {d.column}"

    def test_operation_step_not_mapping_has_line_column(self):
        """Operation step not mapping has non-None line/column."""
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n    to: S\n    transaction:\n      steps:\n        - not-a-map\n  - kind: task_type\n    id: TT\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert result.document is None
        wrong_type = [d for d in result.diagnostics if d.code == "workflow.shape.wrong_type"]
        assert len(wrong_type) >= 1
        d = wrong_type[0]
        assert d.line is not None and d.line >= 1, f"line should be populated, got {d.line}"
        assert d.column is not None and d.column >= 1, f"column should be populated, got {d.column}"

    def test_non_string_requirement_key_is_shape_error(self):
        """Non-string task_type requirement key is a shape workflow.shape.wrong_type and document is None."""
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      123:\n        artifacts: []\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert result.document is None
        wrong_type = [d for d in result.diagnostics if d.code == "workflow.shape.wrong_type"]
        assert len(wrong_type) >= 1, f"expected wrong_type diagnostic, got {[d.code for d in result.diagnostics]}"

    def test_top_level_schema_version_wrong_type_path(self):
        """Top-level schema_version: '1' reports path == 'schema_version'."""
        parsed = parse_yaml("schema_version: '1'\nstatements: []\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        wrong_type = [d for d in result.diagnostics if d.code == "workflow.shape.wrong_type"]
        assert len(wrong_type) >= 1
        d = wrong_type[0]
        assert d.path == "schema_version", f"expected schema_version, got {d.path}"

    def test_unknown_top_level_key_reports_key_path(self):
        """Unknown top-level key reports that key path, not the filename."""
        parsed = parse_yaml("schema_version: 1\nstatements: []\nextra_key: true\n", source_name="myfile.yaml")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        unknown = [d for d in result.diagnostics if d.code == "workflow.shape.unknown_key"]
        assert len(unknown) >= 1
        d = unknown[0]
        assert d.path == "extra_key", f"expected extra_key, got {d.path}"
        assert d.path != "myfile.yaml", "path should not be the filename"

    def test_artifact_item_not_string_has_line_column(self):
        """Artifact item not string has non-None line/column."""
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        artifacts:\n          - 123\n  - kind: state\n    id: S\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert result.document is None
        wrong_type = [d for d in result.diagnostics if d.code == "workflow.shape.wrong_type"]
        assert len(wrong_type) >= 1
        d = wrong_type[0]
        assert d.line is not None and d.line >= 1, f"line should be populated, got {d.line}"
        assert d.column is not None and d.column >= 1, f"column should be populated, got {d.column}"

    def test_arg_spec_required_not_bool_has_line_column(self):
        """arg spec required not boolean has non-None line/column."""
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: validation_type\n    id: v\n    args:\n      x:\n        type: string\n        required: yes\n")
        assert parsed.value is not None
        result = build_ast(parsed.value)
        assert result.document is None
        wrong_type = [d for d in result.diagnostics if d.code == "workflow.shape.wrong_type"]
        assert len(wrong_type) >= 1
        d = wrong_type[0]
        assert d.line is not None and d.line >= 1, f"line should be populated, got {d.line}"
        assert d.column is not None and d.column >= 1, f"column should be populated, got {d.column}"
