from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Literal

from .ast import (
    ArgSpec,
    ArtifactTypeStatement,
    AstVisitor,
    OperationCall,
    OperationTypeStatement,
    RequirementSet,
    StateStatement,
    Statement,
    TaskTypeStatement,
    TransactionPlan,
    TransitionStatement,
    ValidationCall,
    ValidationTypeStatement,
    WorkerStatement,
    WorkflowDocument,
)
from .diagnostics import Diagnostic, SourceSpan
from .visitors import InterpretationVisitor, ValidationVisitor


@dataclasses.dataclass(frozen=True)
class ParseResult:
    value: object | None
    diagnostics: tuple[Diagnostic, ...]


@dataclasses.dataclass(frozen=True)
class AstBuildResult:
    document: WorkflowDocument | None
    diagnostics: tuple[Diagnostic, ...]


@dataclasses.dataclass(frozen=True)
class ValidationResult:
    valid: bool
    diagnostics: tuple[Diagnostic, ...]


def parse_yaml(source: str, *, source_name: str = "<memory>") -> ParseResult:
    from .loader import parse_yaml as _parse_yaml
    return _parse_yaml(source, source_name=source_name)


def load_yaml(path: str | Path) -> ParseResult:
    from .loader import load_yaml as _load_yaml
    return _load_yaml(path)


def build_ast(parsed: object | None, *, source_name: str = "<memory>") -> AstBuildResult:
    from .loader import build_ast as _build_ast
    return _build_ast(parsed, source_name=source_name)


def validate(document: WorkflowDocument) -> ValidationResult:
    from .validation import validate as _validate
    return _validate(document)


def generate_json_schema() -> dict:
    from .schema import generate_json_schema as _generate
    return _generate()


def write_json_schema(path: str | Path) -> None:
    from .schema import write_json_schema as _write
    return _write(path)


__all__ = [
    "ParseResult",
    "AstBuildResult",
    "ValidationResult",
    "Diagnostic",
    "SourceSpan",
    "WorkflowDocument",
    "Statement",
    "StateStatement",
    "TaskTypeStatement",
    "ArtifactTypeStatement",
    "ValidationTypeStatement",
    "WorkerStatement",
    "OperationTypeStatement",
    "TransitionStatement",
    "ArgSpec",
    "ValidationCall",
    "RequirementSet",
    "OperationCall",
    "TransactionPlan",
    "AstVisitor",
    "ValidationVisitor",
    "InterpretationVisitor",
    "parse_yaml",
    "load_yaml",
    "build_ast",
    "validate",
    "generate_json_schema",
    "write_json_schema",
]
