from __future__ import annotations

import dataclasses
from typing import Mapping, Protocol


from .diagnostics import SourceSpan


@dataclasses.dataclass(frozen=True, kw_only=True)
class ArgSpec:
    type: str
    required: bool = False
    many: bool = False
    span: SourceSpan | None = None

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_arg_spec(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class ValidationCall:
    type: str
    args: Mapping[str, object] = dataclasses.field(default_factory=dict)
    span: SourceSpan | None = None
    arg_spans: Mapping[str, SourceSpan] = dataclasses.field(default_factory=dict)

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_validation_call(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class RequirementSet:
    artifacts: tuple[str, ...] = ()
    validations: tuple[ValidationCall, ...] = ()
    span: SourceSpan | None = None
    artifact_spans: tuple[SourceSpan | None, ...] = ()

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_requirement_set(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class OperationCall:
    op: str
    args: Mapping[str, object] = dataclasses.field(default_factory=dict)
    span: SourceSpan | None = None
    arg_spans: Mapping[str, SourceSpan] = dataclasses.field(default_factory=dict)

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_operation_call(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class TransactionPlan:
    steps: tuple[OperationCall, ...]
    span: SourceSpan | None = None

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_transaction_plan(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class ObsidianStateMapping:
    state: str
    board: str
    column: str
    span: SourceSpan | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class ObsidianStorage:
    boards: Mapping[str, str] = dataclasses.field(default_factory=dict)
    state_mappings: tuple[ObsidianStateMapping, ...] = ()
    span: SourceSpan | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class WorkflowStorage:
    obsidian: ObsidianStorage | None = None
    span: SourceSpan | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class WorkflowDocument:
    schema_version: int
    statements: tuple["Statement", ...]
    storage: WorkflowStorage | None = None
    span: SourceSpan | None = None

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_document(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class Statement:
    id: str
    span: SourceSpan | None = None

    def accept(self, visitor: AstVisitor) -> object:
        ...


@dataclasses.dataclass(frozen=True, kw_only=True)
class StateStatement(Statement):
    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_state(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class TaskTypeStatement(Statement):
    requirements_by_state: Mapping[str, RequirementSet] = dataclasses.field(default_factory=dict)

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_task_type(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class ArtifactTypeStatement(Statement):
    template: str | None = None

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_artifact_type(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class ValidationTypeStatement(Statement):
    args: Mapping[str, ArgSpec] = dataclasses.field(default_factory=dict)

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_validation_type(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class WorkerStatement(Statement):
    type: str | None = None

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_worker(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class OperationTypeStatement(Statement):
    args: Mapping[str, ArgSpec] = dataclasses.field(default_factory=dict)

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_operation_type(self)


@dataclasses.dataclass(frozen=True, kw_only=True)
class TransitionStatement(Statement):
    task_type: str
    from_state: str
    to_state: str
    worker: str | None = None
    default_for_scheduler: bool = False
    requires: RequirementSet = dataclasses.field(default_factory=RequirementSet)
    transaction: TransactionPlan | None = None
    field_spans: Mapping[str, SourceSpan] = dataclasses.field(default_factory=dict)

    def accept(self, visitor: AstVisitor) -> object:
        return visitor.visit_transition(self)


class AstVisitor(Protocol):
    def visit_document(self, node: WorkflowDocument) -> object: ...
    def visit_state(self, node: StateStatement) -> object: ...
    def visit_task_type(self, node: TaskTypeStatement) -> object: ...
    def visit_artifact_type(self, node: ArtifactTypeStatement) -> object: ...
    def visit_validation_type(self, node: ValidationTypeStatement) -> object: ...
    def visit_worker(self, node: WorkerStatement) -> object: ...
    def visit_operation_type(self, node: OperationTypeStatement) -> object: ...
    def visit_transition(self, node: TransitionStatement) -> object: ...
    def visit_arg_spec(self, node: ArgSpec) -> object: ...
    def visit_validation_call(self, node: ValidationCall) -> object: ...
    def visit_requirement_set(self, node: RequirementSet) -> object: ...
    def visit_operation_call(self, node: OperationCall) -> object: ...
    def visit_transaction_plan(self, node: TransactionPlan) -> object: ...
