from __future__ import annotations

from pathlib import Path

from . import langdef


def generate_json_schema() -> dict:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Workflow DSL",
        "type": "object",
        "required": ["schema_version", "statements"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "type": "integer",
                "enum": sorted(langdef.SUPPORTED_SCHEMA_VERSIONS),
            },
            "statements": {
                "type": "array",
                "items": {
                    "oneOf": [_statement_schema(kind) for kind in sorted(langdef.SUPPORTED_KINDS)],
                },
            },
            "storage": {
                "type": "object",
                "additionalProperties": True,
            },
        },
    }
    return schema


def _statement_schema(kind: str) -> dict:
    """Generate a JSON Schema object for a statement kind from langdef."""
    allowed = langdef.STATEMENT_KEYS[kind]
    required = sorted(langdef.STATEMENT_REQUIRED_KEYS[kind])
    props = _build_statement_properties(kind, allowed)

    return {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "properties": props,
    }


def _build_statement_properties(kind: str, allowed: frozenset) -> dict:
    """Build the properties dict for a statement kind from langdef data."""
    props: dict[str, dict] = {
        "kind": {"type": "string", "const": kind},
        "id": {"type": "string"},
    }

    # Add optional string fields
    for field_name in ("template", "type"):
        if field_name in allowed:
            props[field_name] = {"type": "string"}

    # Add args mapping for validation_type and operation_type
    if "args" in allowed:
        props["args"] = _arg_specs_schema()

    # Add requirements mapping for task_type
    if "requirements" in allowed:
        props["requirements"] = {
            "type": "object",
            "propertyNames": {"type": "string"},
            "additionalProperties": _requirement_set_schema(),
        }
    if "instructions" in allowed:
        props["instructions"] = {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        }

    # Add transition-specific fields
    transition_fields = {
        "task_type", "from", "to", "worker", "default_for_scheduler", "requires", "transaction",
    }
    for field_name in sorted(transition_fields & allowed):
        if field_name in ("task_type", "from", "to", "worker"):
            props[field_name] = {"type": "string"}
        elif field_name == "default_for_scheduler":
            props[field_name] = {"type": "boolean"}
        elif field_name == "requires":
            props[field_name] = _requirement_set_schema()
        elif field_name == "transaction":
            props[field_name] = _transaction_schema()
    if "derives" in allowed:
        props["derives"] = {
            "type": "object",
            "required": ["task_type", "state", "artifact_type"],
            "additionalProperties": False,
            "properties": {
                "task_type": {"type": "string"},
                "state": {"type": "string"},
                "artifact_type": {"type": "string"},
            },
        }

    return props


def _arg_specs_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": {
            "type": "object",
            "required": ["type"],
            "additionalProperties": False,
            "properties": {
                "type": {
                    "type": "string",
                    "enum": sorted(langdef.SUPPORTED_ARG_TYPES),
                },
                "required": {"type": "boolean"},
                "many": {"type": "boolean"},
            },
        },
    }


def _requirement_set_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
            },
            "validations": {
                "type": "array",
                "items": _validation_call_schema(),
            },
            "changed_files": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "required": {"type": "boolean"},
                },
            },
        },
    }


def _validation_call_schema() -> dict:
    return {
        "type": "object",
        "required": ["type"],
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string"},
            "args": {"type": "object"},
        },
    }


def _transaction_schema() -> dict:
    return {
        "type": "object",
        "required": ["steps"],
        "additionalProperties": False,
        "properties": {
            "steps": {
                "type": "array",
                "items": _operation_call_schema(),
            },
        },
    }


def _operation_call_schema() -> dict:
    return {
        "type": "object",
        "required": ["op"],
        "additionalProperties": False,
        "properties": {
            "op": {"type": "string"},
            "args": {"type": "object"},
        },
    }


def write_json_schema(path: str | Path) -> None:
    import json
    schema = generate_json_schema()
    p = Path(path)
    p.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
