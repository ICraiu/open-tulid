from __future__ import annotations

from . import langdef
from .ast import (
    ArgSpec,
    ArtifactTypeStatement,
    OperationCall,
    OperationTypeStatement,
    RequirementSet,
    StateStatement,
    TaskTypeStatement,
    TransactionPlan,
    TransitionStatement,
    ValidationCall,
    ValidationTypeStatement,
    WorkerStatement,
    WorkflowDocument,
)
from .diagnostics import Diagnostic, SourceSpan
from .symbols import SymbolTable


def _span_info(span: SourceSpan | None) -> tuple[str | None, int | None, int | None]:
    """Extract path, line, column from a SourceSpan."""
    if span is None:
        return None, None, None
    return span.path, span.line, span.column


class ValidationVisitor:
    def __init__(self, table: SymbolTable) -> None:
        self.table = table
        self.diagnostics: list[Diagnostic] = []

    def visit_document(self, node: WorkflowDocument) -> list[Diagnostic]:
        for stmt in node.statements:
            stmt.accept(self)
        return self.diagnostics

    def visit_state(self, node: StateStatement) -> None:
        pass

    def visit_task_type(self, node: TaskTypeStatement) -> None:
        for state_name, req_set in node.requirements_by_state.items():
            # Use the requirement set's span for state reference errors
            span = req_set.span if req_set.span else node.span
            if state_name not in self.table.states:
                self.diagnostics.append(Diagnostic(
                    code="workflow.reference.unknown_state",
                    message=f"task_type {node.id!r} references unknown state: {state_name!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
            req_set.accept(self)

    def visit_artifact_type(self, node: ArtifactTypeStatement) -> None:
        pass

    def visit_validation_type(self, node: ValidationTypeStatement) -> None:
        for arg_name, arg_spec in node.args.items():
            arg_spec.accept(self)

    def visit_worker(self, node: WorkerStatement) -> None:
        pass

    def visit_operation_type(self, node: OperationTypeStatement) -> None:
        for arg_name, arg_spec in node.args.items():
            arg_spec.accept(self)

    def visit_transition(self, node: TransitionStatement) -> None:
        field_spans = node.field_spans if node.field_spans else {}

        # Use field-specific spans for semantic diagnostics
        task_type_span = field_spans.get("task_type", node.span)
        from_span = field_spans.get("from", node.span)
        to_span = field_spans.get("to", node.span)
        worker_span = field_spans.get("worker", node.span)

        if node.task_type not in self.table.task_types:
            p, l, c = _span_info(task_type_span)
            self.diagnostics.append(Diagnostic(
                code="workflow.reference.unknown_task_type",
                message=f"transition {node.id!r} references unknown task_type: {node.task_type!r}",
                path=p,
                line=l,
                column=c,
            ))

        if node.from_state not in self.table.states:
            p, l, c = _span_info(from_span)
            self.diagnostics.append(Diagnostic(
                code="workflow.reference.unknown_state",
                message=f"transition {node.id!r} references unknown from state: {node.from_state!r}",
                path=p,
                line=l,
                column=c,
            ))

        if node.to_state not in self.table.states:
            p, l, c = _span_info(to_span)
            self.diagnostics.append(Diagnostic(
                code="workflow.reference.unknown_state",
                message=f"transition {node.id!r} references unknown to state: {node.to_state!r}",
                path=p,
                line=l,
                column=c,
            ))

        if node.worker is not None and node.worker not in self.table.workers:
            p, l, c = _span_info(worker_span)
            self.diagnostics.append(Diagnostic(
                code="workflow.reference.unknown_worker",
                message=f"transition {node.id!r} references unknown worker: {node.worker!r}",
                path=p,
                line=l,
                column=c,
            ))

        node.requires.accept(self)

        if node.transaction is not None:
            node.transaction.accept(self)

    def visit_arg_spec(self, node: ArgSpec) -> None:
        pass

    def visit_validation_call(self, node: ValidationCall) -> None:
        span = node.span

        if node.type not in self.table.validation_types:
            self.diagnostics.append(Diagnostic(
                code="workflow.reference.unknown_validation",
                message=f"validation call references unknown validation_type: {node.type!r}",
                path=span.path if span else None,
                line=span.line if span else None,
                column=span.column if span else None,
            ))
            return

        val_type = self.table.validation_types[node.type]
        self._validate_call_args(node.args, val_type.args, node, "validation")

    def visit_requirement_set(self, node: RequirementSet) -> None:
        span = node.span

        for art_id in node.artifacts:
            if art_id not in self.table.artifact_types:
                self.diagnostics.append(Diagnostic(
                    code="workflow.reference.unknown_artifact",
                    message=f"requirement set references unknown artifact: {art_id!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))

        for val_call in node.validations:
            val_call.accept(self)

    def visit_operation_call(self, node: OperationCall) -> None:
        span = node.span

        if node.op not in self.table.operation_types:
            self.diagnostics.append(Diagnostic(
                code="workflow.reference.unknown_operation",
                message=f"operation call references unknown operation_type: {node.op!r}",
                path=span.path if span else None,
                line=span.line if span else None,
                column=span.column if span else None,
            ))
            return

        op_type = self.table.operation_types[node.op]
        self._validate_call_args(node.args, op_type.args, node, "operation")

    def visit_transaction_plan(self, node: TransactionPlan) -> None:
        for step in node.steps:
            step.accept(self)

    def _validate_call_args(
        self,
        supplied_args: dict[str, object],
        declared_args: dict[str, ArgSpec],
        call_node,
        call_kind: str,
    ) -> None:
        span = getattr(call_node, "span", None)

        for arg_name, arg_spec in declared_args.items():
            if arg_spec.required and arg_name not in supplied_args:
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.missing_required_argument",
                    message=f"{call_kind} call missing required argument: {arg_name!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))

        for arg_name in supplied_args:
            if arg_name not in declared_args:
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.unknown_argument",
                    message=f"{call_kind} call has unknown argument: {arg_name!r}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))

        for arg_name, arg_spec in declared_args.items():
            if arg_name not in supplied_args:
                continue
            value = supplied_args[arg_name]
            self._validate_arg_value(value, arg_spec, arg_name, call_kind, span)

    def _validate_arg_value(
        self,
        value: object,
        arg_spec: ArgSpec,
        arg_name: str,
        call_kind: str,
        span,
    ) -> None:
        if arg_spec.many:
            if not isinstance(value, list):
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.wrong_argument_type",
                    message=f"{call_kind} arg {arg_name!r} with many:true must be a list, got {type(value).__name__}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
                return
            items = value
        else:
            if isinstance(value, list):
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.wrong_argument_type",
                    message=f"{call_kind} arg {arg_name!r} with many:false must be a scalar, got list",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
                return
            items = [value]

        for item in items:
            self._validate_single_value(item, arg_spec, arg_name, call_kind, span)

    def _validate_single_value(
        self,
        value: object,
        arg_spec: ArgSpec,
        arg_name: str,
        call_kind: str,
        span,
    ) -> None:
        arg_type = arg_spec.type

        if arg_type == "string":
            if not isinstance(value, str):
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.wrong_argument_type",
                    message=f"{call_kind} arg {arg_name!r} expects string, got {type(value).__name__}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
        elif arg_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.wrong_argument_type",
                    message=f"{call_kind} arg {arg_name!r} expects integer, got {type(value).__name__}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
        elif arg_type == "boolean":
            if not isinstance(value, bool):
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.wrong_argument_type",
                    message=f"{call_kind} arg {arg_name!r} expects boolean, got {type(value).__name__}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
        elif arg_type in langdef.REFERENCE_ARG_TYPES:
            if not isinstance(value, str):
                self.diagnostics.append(Diagnostic(
                    code="workflow.call.wrong_argument_type",
                    message=f"{call_kind} arg {arg_name!r} expects string reference, got {type(value).__name__}",
                    path=span.path if span else None,
                    line=span.line if span else None,
                    column=span.column if span else None,
                ))
                return
            ref_kind = langdef.REFERENCE_ARG_TYPES[arg_type]
            self._check_reference(value, arg_type, arg_name, call_kind, span, ref_kind)
        else:
            self.diagnostics.append(Diagnostic(
                code="workflow.call.unknown_argument_type",
                message=f"unknown arg type: {arg_type!r}",
                path=span.path if span else None,
                line=span.line if span else None,
                column=span.column if span else None,
            ))

    def _check_reference(
        self,
        value: str,
        arg_type: str,
        arg_name: str,
        call_kind: str,
        span,
        ref_kind: str,
    ) -> None:
        table_map = {
            "state": self.table.states,
            "task_type": self.table.task_types,
            "artifact_type": self.table.artifact_types,
            "validation_type": self.table.validation_types,
            "worker": self.table.workers,
            "operation_type": self.table.operation_types,
        }
        ref_table = table_map[ref_kind]

        code_map = {
            "state": "workflow.reference.unknown_state",
            "task_type": "workflow.reference.unknown_task_type",
            "artifact_type": "workflow.reference.unknown_artifact",
            "validation_type": "workflow.reference.unknown_validation",
            "worker": "workflow.reference.unknown_worker",
            "operation_type": "workflow.reference.unknown_operation",
        }

        if value not in ref_table:
            self.diagnostics.append(Diagnostic(
                code=code_map[ref_kind],
                message=f"{call_kind} arg {arg_name!r} references unknown {ref_kind}: {value!r}",
                path=span.path if span else None,
                line=span.line if span else None,
                column=span.column if span else None,
            ))


class InterpretationVisitor:
    """Placeholder visitor for future runtime execution. Does not execute anything."""

    def visit_document(self, node: WorkflowDocument) -> object:
        return {"type": "document", "version": node.schema_version, "statement_count": len(node.statements)}

    def visit_state(self, node: StateStatement) -> object:
        return {"type": "state", "id": node.id}

    def visit_task_type(self, node: TaskTypeStatement) -> object:
        return {"type": "task_type", "id": node.id}

    def visit_artifact_type(self, node: ArtifactTypeStatement) -> object:
        return {"type": "artifact_type", "id": node.id}

    def visit_validation_type(self, node: ValidationTypeStatement) -> object:
        return {"type": "validation_type", "id": node.id}

    def visit_worker(self, node: WorkerStatement) -> object:
        return {"type": "worker", "id": node.id}

    def visit_operation_type(self, node: OperationTypeStatement) -> object:
        return {"type": "operation_type", "id": node.id}

    def visit_transition(self, node: TransitionStatement) -> object:
        return {"type": "transition", "id": node.id}

    def visit_arg_spec(self, node: ArgSpec) -> object:
        return {"type": "arg_spec", "arg_type": node.type}

    def visit_validation_call(self, node: ValidationCall) -> object:
        return {"type": "validation_call", "validation_type": node.type}

    def visit_requirement_set(self, node: RequirementSet) -> object:
        return {"type": "requirement_set", "artifact_count": len(node.artifacts)}

    def visit_operation_call(self, node: OperationCall) -> object:
        return {"type": "operation_call", "op": node.op}

    def visit_transaction_plan(self, node: TransactionPlan) -> object:
        return {"type": "transaction_plan", "step_count": len(node.steps)}
