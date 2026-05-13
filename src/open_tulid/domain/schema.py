from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping


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


class ExecutionJobStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
class ExecutionJob:
    job_id: str
    project_id: str
    task_id: str
    transition_id: str
    worker_id: str
    workspace_path: str
    status: ExecutionJobStatus | str = ExecutionJobStatus.CREATED
    attempts: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


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
