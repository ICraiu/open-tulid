from __future__ import annotations

import json
from pathlib import Path

import pytest

import workflow_engine
from workflow_engine import (
    generate_json_schema,
    write_json_schema,
    parse_yaml,
    load_yaml,
    build_ast,
)

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestJsonSchema:
    def test_generate_returns_dict(self):
        schema = generate_json_schema()
        assert isinstance(schema, dict)

    def test_schema_has_required_fields(self):
        schema = generate_json_schema()
        assert "type" in schema
        assert schema["type"] == "object"
        assert "required" in schema
        assert "schema_version" in schema["required"]
        assert "statements" in schema["required"]

    def test_schema_additional_properties_false(self):
        schema = generate_json_schema()
        assert schema["additionalProperties"] is False

    def test_schema_validates_valid_minimal(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n")
        assert parsed.value is not None
        jsonschema.validate(parsed.value, schema)

    def test_schema_validates_valid_full(self):
        schema = generate_json_schema()
        result = load_yaml(str(FIXTURES / "valid_full.yaml"))
        assert result.value is not None
        jsonschema.validate(result.value, schema)

    def test_schema_rejects_missing_schema_version(self):
        schema = generate_json_schema()
        parsed = parse_yaml("statements: []\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_missing_statements(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_unknown_top_level_key(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements: []\nextra: true\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_unknown_kind(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state_type\n    id: Todo\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_unknown_statement_key(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n    extra: true\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_includes_all_kinds(self):
        schema = generate_json_schema()
        stmts_schema = schema["properties"]["statements"]["items"]
        kinds = set()
        for one_of in stmts_schema["oneOf"]:
            if "properties" in one_of and "kind" in one_of["properties"]:
                kind_val = one_of["properties"]["kind"].get("const")
                if kind_val:
                    kinds.add(kind_val)
        expected = {"state", "task_type", "artifact_type", "validation_type", "worker", "operation_type", "transition"}
        assert kinds == expected

    def test_write_json_schema(self, tmp_path):
        out = tmp_path / "schema.json"
        write_json_schema(str(out))
        assert out.exists()
        data = json.loads(out.read_text())
        assert isinstance(data, dict)
        assert data["type"] == "object"

    def test_schema_generated_from_langdef(self):
        schema = generate_json_schema()
        stmts_schema = schema["properties"]["statements"]["items"]
        kinds = set()
        for one_of in stmts_schema["oneOf"]:
            if "properties" in one_of and "kind" in one_of["properties"]:
                kind_val = one_of["properties"]["kind"].get("const")
                if kind_val:
                    kinds.add(kind_val)
        from workflow_engine import langdef
        assert kinds == langdef.SUPPORTED_KINDS

    def test_schema_rejects_transition_missing_task_type(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    from: S\n    to: S\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_transition_missing_from(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    to: S\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_transition_missing_to(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_arg_spec_missing_type(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: validation_type\n    id: v\n    args:\n      x:\n        required: true\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_validation_call_missing_type(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        validations:\n          - args: {}\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_operation_call_missing_op(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n    to: S\n    transaction:\n      steps:\n        - args: {}\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_transaction_missing_steps(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: transition\n    id: T\n    task_type: TT\n    from: S\n    to: S\n    transaction: {}\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_artifact_item_not_string(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: task_type\n    id: T\n    requirements:\n      S:\n        artifacts:\n          - 123\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_arg_spec_many_not_boolean(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: operation_type\n    id: bad\n    args:\n      x:\n        type: string\n        many: nope\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)

    def test_schema_rejects_arg_spec_required_not_boolean(self):
        schema = generate_json_schema()
        parsed = parse_yaml("schema_version: 1\nstatements:\n  - kind: validation_type\n    id: v\n    args:\n      x:\n        type: string\n        required: yes\n")
        assert parsed.value is not None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(parsed.value, schema)
