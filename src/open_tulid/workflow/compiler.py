from __future__ import annotations

import dataclasses
from typing import Mapping
from types import MappingProxyType

from workflow_engine.ast import (
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
from workflow_engine.diagnostics import SourceSpan

from .builtins import get_builtin_registries
from open_tulid.domain.schema import (
    ArgDefinition,
    ArtifactTypeDefinition,
    OperationCallDefinition,
    OperationTypeDefinition,
    RequirementDefinition,
    StateDefinition,
    TaskTypeDefinition,
    TransactionDefinition,
    TransitionDefinition,
    ValidationCallDefinition,
    ValidationTypeDefinition,
    WorkflowDefinition,
    WorkerDefinition,
)
from .diagnostics import WorkflowCompileDiagnostic
from .registry import RuntimeRegistries, validate_registries


@dataclasses.dataclass(frozen=True)
class CompileResult:
    definition: WorkflowDefinition | None
    diagnostics: tuple[WorkflowCompileDiagnostic, ...]

    @property
    def valid(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)


def _span_to_diag_fields(span: SourceSpan | None) -> tuple[str | None, int | None, int | None]:
    if span is None:
        return None, None, None
    return span.path, span.line, span.column


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(mapping: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(k): _freeze_value(v) for k, v in mapping.items()})


def _arg_spec_to_def(spec: ArgSpec) -> ArgDefinition:
    return ArgDefinition(
        type=spec.type,
        required=spec.required,
        many=spec.many,
    )


def _requirement_set_to_def(req: RequirementSet) -> RequirementDefinition:
    validations: list[ValidationCallDefinition] = []
    for vc in req.validations:
        validations.append(ValidationCallDefinition(
            type=vc.type,
            args=_freeze_mapping(vc.args),
        ))
    return RequirementDefinition(
        artifacts=req.artifacts,
        validations=tuple(validations),
    )


def _transaction_to_def(txn: TransactionPlan) -> TransactionDefinition:
    steps: list[OperationCallDefinition] = []
    for step in txn.steps:
        steps.append(OperationCallDefinition(
            op=step.op,
            args=_freeze_mapping(step.args),
        ))
    return TransactionDefinition(steps=tuple(steps))


def _validate_requirement_refs_with_spans(
    req_set: RequirementSet,
    artifact_types: dict[str, ArtifactTypeDefinition],
    validation_types: dict[str, ValidationTypeDefinition],
    diagnostics: list[WorkflowCompileDiagnostic],
    context: str,
) -> None:
    """Validate requirement references using AST spans for precise diagnostics."""
    for idx, artifact_id in enumerate(req_set.artifacts):
        if artifact_id not in artifact_types:
            artifact_span = None
            if idx < len(req_set.artifact_spans):
                artifact_span = req_set.artifact_spans[idx]
            path, line, column = _span_to_diag_fields(artifact_span or req_set.span)
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.unknown_artifact_ref",
                message=f"{context} references unknown artifact_type {artifact_id!r}",
                path=path,
                line=line,
                column=column,
            ))
    for vc in req_set.validations:
        if vc.type not in validation_types:
            path, line, column = _span_to_diag_fields(vc.span or req_set.span)
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.unknown_validation_ref",
                message=f"{context} references unknown validation_type {vc.type!r}",
                path=path,
                line=line,
                column=column,
            ))


def _validate_cross_references_with_ast(
    states: dict[str, StateDefinition],
    task_types: dict[str, TaskTypeDefinition],
    artifact_types: dict[str, ArtifactTypeDefinition],
    validation_types: dict[str, ValidationTypeDefinition],
    operation_types: dict[str, OperationTypeDefinition],
    workers: dict[str, WorkerDefinition],
    transitions: dict[str, TransitionDefinition],
    task_type_stmts: dict[str, TaskTypeStatement],
    transition_stmts: dict[str, TransitionStatement],
    diagnostics: list[WorkflowCompileDiagnostic],
) -> None:
    """Validate cross-references using original AST statements for span info."""
    for tt_id, tt_stmt in task_type_stmts.items():
        tt_def = task_types[tt_id]
        for state_name, req_set in tt_stmt.requirements_by_state.items():
            if state_name not in states:
                path, line, column = _span_to_diag_fields(req_set.span or tt_stmt.span)
                diagnostics.append(WorkflowCompileDiagnostic(
                    code="workflow.compile.unknown_state_ref",
                    message=f"task_type {tt_def.id!r} references unknown state {state_name!r}",
                    path=path,
                    line=line,
                    column=column,
                ))
            ctx = f"task_type {tt_def.id!r} state {state_name!r}"
            _validate_requirement_refs_with_spans(
                req_set, artifact_types, validation_types, diagnostics, ctx,
            )

    for trans_id, trans_stmt in transition_stmts.items():
        trans = transitions[trans_id]
        ctx = f"transition {trans.id!r}"

        task_type_span = trans_stmt.field_spans.get("task_type")
        if trans.task_type not in task_types:
            path, line, column = _span_to_diag_fields(task_type_span or trans_stmt.span)
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.unknown_task_type_ref",
                message=f"{ctx} references unknown task_type {trans.task_type!r}",
                path=path,
                line=line,
                column=column,
            ))

        from_span = trans_stmt.field_spans.get("from")
        if trans.from_state not in states:
            path, line, column = _span_to_diag_fields(from_span or trans_stmt.span)
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.unknown_state_ref",
                message=f"{ctx} references unknown from_state {trans.from_state!r}",
                path=path,
                line=line,
                column=column,
            ))

        to_span = trans_stmt.field_spans.get("to")
        if trans.to_state not in states:
            path, line, column = _span_to_diag_fields(to_span or trans_stmt.span)
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.unknown_state_ref",
                message=f"{ctx} references unknown to_state {trans.to_state!r}",
                path=path,
                line=line,
                column=column,
            ))

        worker_span = trans_stmt.field_spans.get("worker")
        if trans.worker is not None and trans.worker not in workers:
            path, line, column = _span_to_diag_fields(worker_span or trans_stmt.span)
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.unknown_worker_ref",
                message=f"{ctx} references unknown worker {trans.worker!r}",
                path=path,
                line=line,
                column=column,
            ))

        _validate_requirement_refs_with_spans(
            trans_stmt.requires, artifact_types, validation_types, diagnostics, ctx,
        )

        if trans_stmt.transaction is not None:
            for step in trans_stmt.transaction.steps:
                if step.op not in operation_types:
                    path, line, column = _span_to_diag_fields(step.span or trans_stmt.span)
                    diagnostics.append(WorkflowCompileDiagnostic(
                        code="workflow.compile.unknown_operation_ref",
                        message=f"{ctx} transaction references unknown operation_type {step.op!r}",
                        path=path,
                        line=line,
                        column=column,
                    ))


def compile_workflow(
    document: WorkflowDocument,
    registries: RuntimeRegistries | None = None,
) -> CompileResult:
    """Compile a WorkflowDocument into a WorkflowDefinition.

    Validates:
    - Registry integrity (duplicate IDs, missing implementations, arg types).
    - Validation/operation/worker types are registered.
    - Cross-references: transitions reference declared states, task_types,
      workers, artifact_types, validation_types, and operation_types.
    - TaskType requirements reference declared states, artifact_types,
      and validation_types.

    Artifact templates are compiled as opaque references owned by the DSL.
    """
    if registries is None:
        registries = get_builtin_registries()

    reg_diagnostics = validate_registries(registries)
    if reg_diagnostics:
        return CompileResult(definition=None, diagnostics=reg_diagnostics)

    diagnostics: list[WorkflowCompileDiagnostic] = []
    states: dict[str, StateDefinition] = {}
    task_types: dict[str, TaskTypeDefinition] = {}
    artifact_types: dict[str, ArtifactTypeDefinition] = {}
    validation_types: dict[str, ValidationTypeDefinition] = {}
    operation_types: dict[str, OperationTypeDefinition] = {}
    workers: dict[str, WorkerDefinition] = {}
    transitions: dict[str, TransitionDefinition] = {}

    task_type_stmts: dict[str, TaskTypeStatement] = {}
    transition_stmts: dict[str, TransitionStatement] = {}

    for stmt in document.statements:
        if isinstance(stmt, StateStatement):
            states[stmt.id] = StateDefinition(id=stmt.id)

        elif isinstance(stmt, TaskTypeStatement):
            reqs: dict[str, RequirementDefinition] = {}
            for state_name, req_set in stmt.requirements_by_state.items():
                reqs[state_name] = _requirement_set_to_def(req_set)
            task_types[stmt.id] = TaskTypeDefinition(
                id=stmt.id,
                requirements_by_state=_freeze_mapping(reqs),
            )
            task_type_stmts[stmt.id] = stmt

        elif isinstance(stmt, ArtifactTypeStatement):
            artifact_types[stmt.id] = ArtifactTypeDefinition(
                id=stmt.id,
                template=stmt.template,
            )

        elif isinstance(stmt, ValidationTypeStatement):
            impl_id = stmt.id
            if impl_id not in registries.validations:
                path, line, column = _span_to_diag_fields(stmt.span)
                diagnostics.append(WorkflowCompileDiagnostic(
                    code="workflow.compile.unsupported_validation",
                    message=f"validation_type {impl_id!r} is not registered",
                    path=path,
                    line=line,
                    column=column,
                ))
            args_def: dict[str, ArgDefinition] = {}
            for arg_name, arg_spec in stmt.args.items():
                args_def[arg_name] = _arg_spec_to_def(arg_spec)
            validation_types[stmt.id] = ValidationTypeDefinition(
                id=stmt.id,
                args=_freeze_mapping(args_def),
                implementation_id=impl_id,
            )

        elif isinstance(stmt, OperationTypeStatement):
            impl_id = stmt.id
            if impl_id not in registries.operations:
                path, line, column = _span_to_diag_fields(stmt.span)
                diagnostics.append(WorkflowCompileDiagnostic(
                    code="workflow.compile.unsupported_operation",
                    message=f"operation_type {impl_id!r} is not registered",
                    path=path,
                    line=line,
                    column=column,
                ))
            args_def: dict[str, ArgDefinition] = {}
            for arg_name, arg_spec in stmt.args.items():
                args_def[arg_name] = _arg_spec_to_def(arg_spec)
            operation_types[stmt.id] = OperationTypeDefinition(
                id=stmt.id,
                args=_freeze_mapping(args_def),
                implementation_id=impl_id,
            )

        elif isinstance(stmt, WorkerStatement):
            impl_id = stmt.type if stmt.type is not None else stmt.id
            if impl_id not in registries.workers:
                path, line, column = _span_to_diag_fields(stmt.span)
                diagnostics.append(WorkflowCompileDiagnostic(
                    code="workflow.compile.unsupported_worker",
                    message=f"worker {stmt.id!r} uses unsupported implementation {impl_id!r}",
                    path=path,
                    line=line,
                    column=column,
                ))
            workers[stmt.id] = WorkerDefinition(
                id=stmt.id,
                type=stmt.type,
                implementation_id=impl_id,
            )

        elif isinstance(stmt, TransitionStatement):
            req_def = _requirement_set_to_def(stmt.requires)
            txn_def = None
            if stmt.transaction is not None:
                txn_def = _transaction_to_def(stmt.transaction)

            transitions[stmt.id] = TransitionDefinition(
                id=stmt.id,
                task_type=stmt.task_type,
                from_state=stmt.from_state,
                to_state=stmt.to_state,
                worker=stmt.worker,
                requires=req_def,
                transaction=txn_def,
            )
            transition_stmts[stmt.id] = stmt

    _validate_cross_references_with_ast(
        states, task_types, artifact_types,
        validation_types, operation_types, workers,
        transitions, task_type_stmts, transition_stmts,
        diagnostics,
    )

    if diagnostics:
        return CompileResult(definition=None, diagnostics=tuple(diagnostics))

    definition = WorkflowDefinition(
        schema_version=document.schema_version,
        states=_freeze_mapping(states),
        task_types=_freeze_mapping(task_types),
        artifact_types=_freeze_mapping(artifact_types),
        validation_types=_freeze_mapping(validation_types),
        operation_types=_freeze_mapping(operation_types),
        workers=_freeze_mapping(workers),
        transitions=_freeze_mapping(transitions),
    )
    return CompileResult(definition=definition, diagnostics=())
