from __future__ import annotations

from typing import Any

from .ast import (
    ArtifactTypeStatement,
    OperationTypeStatement,
    Statement,
    StateStatement,
    TaskTypeStatement,
    TransitionStatement,
    ValidationTypeStatement,
    WorkerStatement,
    WorkflowDocument,
)
from .diagnostics import Diagnostic


class SymbolTable:
    def __init__(self) -> None:
        self.states: dict[str, StateStatement] = {}
        self.task_types: dict[str, TaskTypeStatement] = {}
        self.artifact_types: dict[str, ArtifactTypeStatement] = {}
        self.validation_types: dict[str, ValidationTypeStatement] = {}
        self.workers: dict[str, WorkerStatement] = {}
        self.operation_types: dict[str, OperationTypeStatement] = {}
        self.transitions: dict[str, TransitionStatement] = {}


def build_symbol_table(document: WorkflowDocument) -> tuple[SymbolTable, list[Diagnostic]]:
    table = SymbolTable()
    diagnostics: list[Diagnostic] = []

    for stmt in document.statements:
        stmt_id = stmt.id
        span = stmt.span

        if isinstance(stmt, StateStatement):
            if stmt_id in table.states:
                diagnostics.append(Diagnostic(
                    code="workflow.symbol.duplicate_id",
                    message=f"duplicate state id: {stmt_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            else:
                table.states[stmt_id] = stmt

        elif isinstance(stmt, TaskTypeStatement):
            if stmt_id in table.task_types:
                diagnostics.append(Diagnostic(
                    code="workflow.symbol.duplicate_id",
                    message=f"duplicate task_type id: {stmt_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            else:
                table.task_types[stmt_id] = stmt

        elif isinstance(stmt, ArtifactTypeStatement):
            if stmt_id in table.artifact_types:
                diagnostics.append(Diagnostic(
                    code="workflow.symbol.duplicate_id",
                    message=f"duplicate artifact_type id: {stmt_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            else:
                table.artifact_types[stmt_id] = stmt

        elif isinstance(stmt, ValidationTypeStatement):
            if stmt_id in table.validation_types:
                diagnostics.append(Diagnostic(
                    code="workflow.symbol.duplicate_id",
                    message=f"duplicate validation_type id: {stmt_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            else:
                table.validation_types[stmt_id] = stmt

        elif isinstance(stmt, WorkerStatement):
            if stmt_id in table.workers:
                diagnostics.append(Diagnostic(
                    code="workflow.symbol.duplicate_id",
                    message=f"duplicate worker id: {stmt_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            else:
                table.workers[stmt_id] = stmt

        elif isinstance(stmt, OperationTypeStatement):
            if stmt_id in table.operation_types:
                diagnostics.append(Diagnostic(
                    code="workflow.symbol.duplicate_id",
                    message=f"duplicate operation_type id: {stmt_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            else:
                table.operation_types[stmt_id] = stmt

        elif isinstance(stmt, TransitionStatement):
            if stmt_id in table.transitions:
                diagnostics.append(Diagnostic(
                    code="workflow.symbol.duplicate_id",
                    message=f"duplicate transition id: {stmt_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            else:
                table.transitions[stmt_id] = stmt

    return table, diagnostics
