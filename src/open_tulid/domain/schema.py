from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping


@dataclass
class ValidationError:
    """One validation problem found in user-facing project data.
    It points at a path/location and carries the message to report."""
    path: str | None
    location: str
    message: str


@dataclass
class ValidationReport:
    """Aggregate validation result for one check or validation pass.
    It is valid only when no errors were collected."""
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, location: str, message: str, path: str | None = None) -> None:
        self.errors.append(ValidationError(path=path, location=location, message=message))


@dataclass(frozen=True)
class Task:
    """Canonical task record used by the domain and runtime.
    It stores stable task identity, workflow state, links, and task body."""
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
    """Location of a task as found in a tracker board file.
    This preserves where the task appeared in the external board representation."""
    board: str
    column: str
    card_text: str
    line: int


@dataclass(frozen=True)
class ProjectSnapshot:
    """In-memory snapshot of a project's task state.
    `tasks` is keyed by task id, and `board_positions` is keyed by task id for tasks that were found on a board."""
    project_id: str
    tasks: Mapping[str, Task]
    board_positions: Mapping[str, BoardPosition]


@dataclass(frozen=True)
class DomainError:
    """Structured domain/runtime error passed between modules.
    It carries a stable code plus a human-readable message and optional location."""
    code: str
    message: str
    location: str | None = None


class EventType(str, Enum):
    """Enumeration of domain event kinds emitted by the system.
    These names define the event stream vocabulary."""
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
    """Lifecycle states for an execution job.
    They describe scheduling, execution, completion submission, and terminal outcomes."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETION_SUBMITTED = "completion_submitted"
    COMPLETION_REJECTED = "completion_rejected"
    ACCEPTED = "accepted"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"

    CREATED = "pending"
    COMPLETED = "accepted"


class JournalStatus(str, Enum):
    """State of a transaction journal record.
    A journal is either prepared, committed, or failed."""
    PREPARED = "prepared"
    COMMITTED = "committed"
    FAILED = "failed"


@dataclass(frozen=True)
class EventActor:
    """Identity of the actor that emitted an event.
    This can represent a system component, user, or tool."""
    type: str
    id: str


@dataclass(frozen=True)
class EventEnvelope:
    """Stored event record with routing and correlation metadata.
    This is the canonical shape written to the event log."""
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
    """Persistent record of a multi-step transition/application attempt.
    It captures intended effects, emitted events, status, and failure details if any."""
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
    """Domain command to move an existing task between workflow states.
    It records the task id and the exact state change being applied."""
    task_id: str
    from_state: str
    to_state: str


@dataclass(frozen=True)
class WriteTask:
    """Domain command to persist a task record.
    It wraps the canonical task object to be written."""
    task: Task


@dataclass(frozen=True)
class ExecutionJob:
    """Unit of scheduled worker execution for one task transition.
    It binds the task, transition, worker, workspace, and execution metadata."""
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
    """Definition of one argument accepted by a validation or operation type.
    It captures its value type and cardinality rules."""
    type: str
    required: bool = False
    many: bool = False


@dataclass(frozen=True)
class WorkflowDefinition:
    """Fully compiled workflow model used by runtime and validation.
    It contains all states, task types, workers, transitions, and optional storage config."""
    schema_version: int
    states: Mapping[str, "StateDefinition"]
    task_types: Mapping[str, "TaskTypeDefinition"]
    artifact_types: Mapping[str, "ArtifactTypeDefinition"]
    validation_types: Mapping[str, "ValidationTypeDefinition"]
    operation_types: Mapping[str, "OperationTypeDefinition"]
    workers: Mapping[str, "WorkerDefinition"]
    transitions: Mapping[str, "TransitionDefinition"]
    storage: "StorageDefinition | None" = None


@dataclass(frozen=True)
class StorageDefinition:
    """Workflow-declared storage configuration.
    Adapters interpret this mapping to bind the project to a concrete tracker/storage backend."""
    config: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class StateDefinition:
    """One named workflow state.
    States are the stable nodes tasks move between."""
    id: str


@dataclass(frozen=True)
class TaskTypeDefinition:
    """Definition of a task category in the workflow.
    It declares per-state requirements and optional instructions for that task type."""
    id: str
    requirements_by_state: Mapping[str, "RequirementDefinition"]
    instructions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactTypeDefinition:
    """Definition of an artifact kind produced or referenced by workflow transitions.
    It may optionally point at a template contract."""
    id: str
    template: str | None = None


@dataclass(frozen=True)
class ValidationTypeDefinition:
    """Definition of a validation capability available in the workflow.
    It names the validation, its arguments, and the implementation id to execute."""
    id: str
    args: Mapping[str, ArgDefinition]
    implementation_id: str


@dataclass(frozen=True)
class OperationTypeDefinition:
    """Definition of an operation capability available in the workflow.
    It names the operation, its arguments, and the implementation id to execute."""
    id: str
    args: Mapping[str, ArgDefinition]
    implementation_id: str


@dataclass(frozen=True)
class WorkerDefinition:
    """Definition of a worker available to transitions.
    It identifies the worker kind, implementation binding, and any attached instructions."""
    id: str
    type: str | None = None
    implementation_id: str | None = None
    instructions: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitionDefinition:
    """One allowed state transition for a task type.
    It defines who executes it, what it requires, and any transaction or derivation behavior."""
    id: str
    task_type: str
    from_state: str
    to_state: str
    worker: str | None
    requires: "RequirementDefinition"
    transaction: "TransactionDefinition" | None
    derives: "DerivesDefinition | None" = None
    default_for_scheduler: bool = False
    instructions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequirementDefinition:
    """Requirements that must be satisfied for a transition to complete.
    This can require artifacts, validation evidence, and changed-file reporting."""
    artifacts: tuple[str, ...] = ()
    validations: tuple["ValidationCallDefinition", ...] = ()
    changed_files_required: bool = False


@dataclass(frozen=True)
class ValidationCallDefinition:
    """Concrete validation invocation inside a transition requirement set.
    It names the validation type and the arguments to pass."""
    type: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class OperationCallDefinition:
    """Concrete operation invocation inside a transaction.
    It names the operation and the arguments to pass."""
    op: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class TransactionDefinition:
    """Ordered set of operations that should be applied as one transition transaction.
    Runtime uses this to prepare, commit, or fail grouped effects consistently."""
    steps: tuple["OperationCallDefinition", ...]


@dataclass(frozen=True)
class DerivesDefinition:
    """Rule describing tasks derived from a transition's output.
    It tells the system what task type, starting state, and artifact type the output creates."""
    task_type: str
    state: str
    artifact_type: str
