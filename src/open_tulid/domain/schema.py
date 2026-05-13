from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping


class ArtifactState(str, Enum):
    IdeaTask = "IdeaTask"
    DefinedTask = "DefinedTask"
    TechnicalDirection = "TechnicalDirection"
    TechnicalSpec = "TechnicalSpec"
    CompletedTask = "CompletedTask"


class FieldType(str, Enum):
    STRING = "STRING"
    FILE = "FILE"
    FILE_LIST = "FILE_LIST"
    STATUS = "STATUS"


class ValidatorType(str, Enum):
    NON_EMPTY_TEXT = "NON_EMPTY_TEXT"
    FILE_LINK_EXISTS = "FILE_LINK_EXISTS"
    FILE_LINK_MATCHES_TEMPLATE = "FILE_LINK_MATCHES_TEMPLATE"
    SECTION_PRESENT = "SECTION_PRESENT"
    REQUIRED_FIELD_PRESENT = "REQUIRED_FIELD_PRESENT"
    TASK_HAS_PROOF_WHEN_DONE = "TASK_HAS_PROOF_WHEN_DONE"


class MappingRuleType(str, Enum):
    CARRY_FIELD = "CARRY_FIELD"
    CREATE_SECTION = "CREATE_SECTION"
    SET_FIELD = "SET_FIELD"
    LINK_ARTIFACT = "LINK_ARTIFACT"


@dataclass
class ValidationError:
    path: str | None
    location: str
    message: str


@dataclass
class ValidationReport:
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, location: str, message: str, path: str | None = None) -> None:
        self.errors.append(ValidationError(path=path, location=location, message=message))


@dataclass
class RequiredWhen:
    field_name: str
    equals: str


@dataclass
class FieldTemplate:
    name: str
    type: FieldType
    required: bool = True
    required_when: RequiredWhen | None = None
    validators: list[ValidatorType] = field(default_factory=list)


@dataclass
class SectionTemplate:
    name: str
    fields: list[FieldTemplate] = field(default_factory=list)
    required: bool = True


@dataclass
class Template:
    name: str
    state: ArtifactState
    sections: list[SectionTemplate] = field(default_factory=list)


@dataclass
class Field:
    name: str
    type: FieldType
    value: str | list[str]


@dataclass
class Section:
    name: str
    fields: list[Field] = field(default_factory=list)


@dataclass
class Artifact:
    path: str
    state: ArtifactState
    template: Template
    sections: list[Section] = field(default_factory=list)
    source_artifacts: list[str] = field(default_factory=list)


@dataclass
class ArtifactRegistry:
    artifacts_by_path: dict[str, Artifact] = field(default_factory=dict)

    def register(self, artifact: Artifact) -> None:
        self.artifacts_by_path[artifact.path] = artifact

    def get(self, path: str) -> Artifact | None:
        return self.artifacts_by_path.get(path)

    def contains(self, path: str) -> bool:
        return path in self.artifacts_by_path


@dataclass
class MappingRule:
    kind: MappingRuleType
    from_section: str | None = None
    from_field: str | None = None
    to_section: str | None = None
    to_field: str | None = None
    value: str | None = None


@dataclass
class Transition:
    name: str
    from_state: ArtifactState
    to_state: ArtifactState
    required_inputs: list[Template] = field(default_factory=list)
    output_template: Template | None = None
    mapping_rules: list[MappingRule] = field(default_factory=list)
    validation_rules: list[ValidatorType] = field(default_factory=list)


@dataclass
class ArtifactReadResult:
    artifact: Artifact | None = None
    report: ValidationReport = field(default_factory=ValidationReport)

    @property
    def is_valid(self) -> bool:
        return self.artifact is not None and self.report.is_valid


@dataclass
class ArtifactWriteResult:
    path: str | None = None
    content: str | None = None
    report: ValidationReport = field(default_factory=ValidationReport)

    @property
    def is_valid(self) -> bool:
        return self.path is not None and self.content is not None and self.report.is_valid


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    path: str
    current_state: str
    task_type: str = "task"
    dependencies: tuple[str, ...] = ()
    artifact_links: tuple[str, ...] = ()
    parent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    body: str = ""


@dataclass(frozen=True)
class BoardPosition:
    board: str
    column: str
    card_text: str
    line: int


@dataclass(frozen=True)
class ProjectSnapshot:
    project_id: str
    tasks: Mapping[str, Task]
    board_positions: Mapping[str, BoardPosition]
    artifacts: tuple[Artifact, ...] = ()


@dataclass(frozen=True)
class DomainError:
    code: str
    message: str
    location: str | None = None


class EventType(str, Enum):
    TaskValidated = "TaskValidated"
    ValidationFailed = "ValidationFailed"
    TransitionRequested = "TransitionRequested"
    TransitionAccepted = "TransitionAccepted"
    TransitionRejected = "TransitionRejected"
    TaskMoved = "TaskMoved"
    ArtifactWritten = "ArtifactWritten"
    ExecutionJobCreated = "ExecutionJobCreated"
    ExecutionStarted = "ExecutionStarted"
    ExecutionCompletionSubmitted = "ExecutionCompletionSubmitted"
    ExecutionCompletionRejected = "ExecutionCompletionRejected"
    ExecutionFinished = "ExecutionFinished"
    ExecutionFailed = "ExecutionFailed"
    ReviewRequested = "ReviewRequested"
    TaskDerived = "TaskDerived"
    TransactionFailed = "TransactionFailed"
    OperationStarted = "OperationStarted"
    OperationFinished = "OperationFinished"
    OperationFailed = "OperationFailed"


class JournalStatus(str, Enum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    FAILED = "failed"


@dataclass(frozen=True)
class EventActor:
    type: str
    id: str


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    schema_version: int
    timestamp: str
    project_id: str
    actor: EventActor
    event_type: EventType | str
    correlation_id: str
    task_id: str | None = None
    job_id: str | None = None
    transition_id: str | None = None
    submission_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransactionJournalRecord:
    journal_id: str
    project_id: str
    started_at: str
    effects: tuple[Mapping[str, Any], ...]
    events: tuple[EventEnvelope, ...]
    status: JournalStatus | Literal["prepared", "committed", "failed"]
    task_id: str | None = None
    transition_id: str | None = None
    completed_at: str | None = None
    error: DomainError | None = None


@dataclass(frozen=True)
class MoveTask:
    task_id: str
    from_state: str
    to_state: str


@dataclass(frozen=True)
class WriteTask:
    task: Task


@dataclass(frozen=True)
class ArgDefinition:
    type: str
    required: bool = False
    many: bool = False


@dataclass(frozen=True)
class WorkflowDefinition:
    schema_version: int
    states: Mapping[str, "StateDefinition"]
    task_types: Mapping[str, "TaskTypeDefinition"]
    artifact_types: Mapping[str, "ArtifactTypeDefinition"]
    validation_types: Mapping[str, "ValidationTypeDefinition"]
    operation_types: Mapping[str, "OperationTypeDefinition"]
    workers: Mapping[str, "WorkerDefinition"]
    transitions: Mapping[str, "TransitionDefinition"]


@dataclass(frozen=True)
class StateDefinition:
    id: str


@dataclass(frozen=True)
class TaskTypeDefinition:
    id: str
    requirements_by_state: Mapping[str, "RequirementDefinition"]


@dataclass(frozen=True)
class ArtifactTypeDefinition:
    id: str
    template: str | None = None
    handler: str | None = None


@dataclass(frozen=True)
class ValidationTypeDefinition:
    id: str
    args: Mapping[str, ArgDefinition]
    implementation_id: str


@dataclass(frozen=True)
class OperationTypeDefinition:
    id: str
    args: Mapping[str, ArgDefinition]
    implementation_id: str


@dataclass(frozen=True)
class WorkerDefinition:
    id: str
    type: str | None = None
    implementation_id: str | None = None


@dataclass(frozen=True)
class TransitionDefinition:
    id: str
    task_type: str
    from_state: str
    to_state: str
    worker: str | None
    requires: "RequirementDefinition"
    transaction: "TransactionDefinition" | None


@dataclass(frozen=True)
class RequirementDefinition:
    artifacts: tuple[str, ...] = ()
    validations: tuple["ValidationCallDefinition", ...] = ()


@dataclass(frozen=True)
class ValidationCallDefinition:
    type: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class OperationCallDefinition:
    op: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class TransactionDefinition:
    steps: tuple["OperationCallDefinition", ...]


COMPATIBLE_VALIDATORS: dict[FieldType, set[ValidatorType]] = {
    FieldType.STRING: {
        ValidatorType.NON_EMPTY_TEXT,
        ValidatorType.REQUIRED_FIELD_PRESENT,
    },
    FieldType.STATUS: {
        ValidatorType.NON_EMPTY_TEXT,
        ValidatorType.REQUIRED_FIELD_PRESENT,
        ValidatorType.TASK_HAS_PROOF_WHEN_DONE,
    },
    FieldType.FILE: {
        ValidatorType.FILE_LINK_EXISTS,
        ValidatorType.FILE_LINK_MATCHES_TEMPLATE,
        ValidatorType.REQUIRED_FIELD_PRESENT,
    },
    FieldType.FILE_LIST: {
        ValidatorType.FILE_LINK_EXISTS,
        ValidatorType.FILE_LINK_MATCHES_TEMPLATE,
        ValidatorType.REQUIRED_FIELD_PRESENT,
    },
}
