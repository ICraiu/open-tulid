from __future__ import annotations

from .ast import WorkflowDocument
from .symbols import SymbolTable, build_symbol_table
from .visitors import ValidationVisitor


def validate(document: WorkflowDocument) -> ValidationResult:
    from . import ValidationResult as VR

    table, symbol_diagnostics = build_symbol_table(document)

    visitor = ValidationVisitor(table)
    visitor.visit_document(document)

    all_diagnostics = symbol_diagnostics + visitor.diagnostics
    valid = not any(d.severity == "error" for d in all_diagnostics)

    return VR(valid=valid, diagnostics=tuple(all_diagnostics))
