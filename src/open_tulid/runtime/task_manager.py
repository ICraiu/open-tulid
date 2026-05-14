from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import (
    DomainError,
    EventActor,
    EventEnvelope,
    EventType,
    ExecutionJob,
    ProjectSnapshot,
    WorkflowDefinition,
)

from .events import build_event, new_ulid


@dataclass(frozen=True)
class RequestTransition:
    project_id: str
    task_id: str
    transition_id: str
    actor: EventActor = field(default_factory=lambda: EventActor(type="system", id="task-manager"))


@dataclass(frozen=True)
class ValidateProject:
    project_id: str


@dataclass(frozen=True)
class CreateExecutionJob:
    project_id: str
    task_id: str
    transition_id: str
    workspace_root: Path
    actor: EventActor = field(default_factory=lambda: EventActor(type="system", id="task-manager"))


@dataclass(frozen=True)
class RecordExecutionResult:
    project_id: str
    job_id: str
    accepted: bool
    message: str = ""
    data: Mapping[str, object] = field(default_factory=dict)
    actor: EventActor = field(default_factory=lambda: EventActor(type="system", id="task-manager"))


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    errors: tuple[DomainError, ...] = ()
    events: tuple[EventEnvelope, ...] = ()
    effects: tuple[Mapping[str, object], ...] = ()
    job: ExecutionJob | None = None


class TaskManager:
    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        adapter: StorageAdapter,
        job_store: object | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store

    def handle(
        self,
        command: RequestTransition | ValidateProject | CreateExecutionJob | RecordExecutionResult,
    ) -> CommandResult:
        if isinstance(command, ValidateProject):
            return self.validate_project(command)
        if isinstance(command, RequestTransition):
            return self.request_transition(command)
        if isinstance(command, CreateExecutionJob):
            return self.create_execution_job(command)
        if isinstance(command, RecordExecutionResult):
            return self.record_execution_result(command)
        return CommandResult(accepted=False, errors=(_error(
            "command.unsupported",
            f"Unsupported command: {type(command).__name__}",
        ),))

    def validate_project(self, command: ValidateProject) -> CommandResult:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return CommandResult(accepted=False, errors=loaded.errors)
        snapshot = loaded.snapshot
        if snapshot is None:
            return CommandResult(accepted=False, errors=(_error(
                "project.snapshot_missing",
                "Adapter returned no project snapshot.",
            ),))
        errors = _validate_snapshot_against_workflow(snapshot, self.workflow)
        return CommandResult(accepted=not errors, errors=tuple(errors))

    def request_transition(self, command: RequestTransition) -> CommandResult:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return CommandResult(accepted=False, errors=loaded.errors)
        snapshot = loaded.snapshot
        if snapshot is None:
            return CommandResult(accepted=False, errors=(_error(
                "project.snapshot_missing",
                "Adapter returned no project snapshot.",
            ),))
        task = snapshot.tasks.get(command.task_id)
        if task is None:
            return CommandResult(accepted=False, errors=(_error(
                "task.not_found",
                f"Task {command.task_id!r} was not found.",
                command.task_id,
            ),))
        transition = self.workflow.transitions.get(command.transition_id)
        if transition is None:
            return CommandResult(accepted=False, errors=(_error(
                "transition.not_found",
                f"Transition {command.transition_id!r} is not defined.",
                command.transition_id,
            ),))
        if transition.task_type != task.task_type:
            return CommandResult(accepted=False, errors=(_error(
                "transition.task_type_mismatch",
                f"Transition {transition.id!r} expects task type {transition.task_type!r}.",
                task.id,
            ),))
        if transition.from_state != task.current_state:
            return CommandResult(accepted=False, errors=(_error(
                "transition.state_mismatch",
                f"Transition {transition.id!r} requires state {transition.from_state!r}.",
                task.id,
            ),))
        events = (_event(
            project_id=command.project_id,
            actor=command.actor,
            event_type=EventType.TransitionAccepted,
            task_id=task.id,
            transition_id=transition.id,
            data={"from": transition.from_state, "to": transition.to_state},
        ),)
        return CommandResult(accepted=True, events=events)

    def create_execution_job(self, command: CreateExecutionJob) -> CommandResult:
        transition_result = self.request_transition(RequestTransition(
            project_id=command.project_id,
            task_id=command.task_id,
            transition_id=command.transition_id,
            actor=command.actor,
        ))
        if not transition_result.accepted:
            return transition_result
        transition = self.workflow.transitions[command.transition_id]
        if transition.worker is None:
            return CommandResult(accepted=False, errors=(_error(
                "transition.worker_missing",
                f"Transition {transition.id!r} has no worker.",
                transition.id,
            ),))
        job_id = new_ulid()
        workspace = command.workspace_root / job_id
        output_path = workspace / "output"
        job = ExecutionJob(
            job_id=job_id,
            project_id=command.project_id,
            task_id=command.task_id,
            transition_id=command.transition_id,
            worker_id=transition.worker,
            workspace_path=str(workspace),
            metadata={
                "completion_token": secrets.token_urlsafe(24),
                "output_path": str(output_path),
            },
        )
        if self.job_store is not None:
            saved = self.job_store.create(job)
            if not saved.accepted:
                return CommandResult(
                    accepted=False,
                    errors=(saved.error or _error("job.write_failed", "Execution job was not recorded."),),
                )
            if saved.job is not None:
                job = saved.job
        events = (
            *transition_result.events,
            _event(
                project_id=command.project_id,
                actor=command.actor,
                event_type=EventType.ExecutionJobCreated,
                task_id=command.task_id,
                transition_id=command.transition_id,
                job_id=job.job_id,
                data={"worker_id": job.worker_id, "workspace_path": job.workspace_path},
            ),
        )
        effects = ({"type": "create_execution_job", "job": _job_to_dict(job)},)
        return CommandResult(accepted=True, events=events, effects=effects, job=job)

    def record_execution_result(self, command: RecordExecutionResult) -> CommandResult:
        event_type = EventType.ExecutionFinished if command.accepted else EventType.ExecutionFailed
        return CommandResult(accepted=True, events=(_event(
            project_id=command.project_id,
            actor=command.actor,
            event_type=event_type,
            job_id=command.job_id,
            data={"message": command.message, **dict(command.data)},
        ),))


def _validate_snapshot_against_workflow(
    snapshot: ProjectSnapshot,
    workflow: WorkflowDefinition,
) -> list[DomainError]:
    errors: list[DomainError] = []
    for task in snapshot.tasks.values():
        if task.task_type not in workflow.task_types:
            errors.append(_error(
                "task.unknown_type",
                f"Task {task.id!r} uses unknown task type {task.task_type!r}.",
                task.id,
            ))
        if task.current_state not in workflow.states:
            errors.append(_error(
                "task.unknown_state",
                f"Task {task.id!r} is in unknown state {task.current_state!r}.",
                task.id,
            ))
    return errors


def _event(
    *,
    project_id: str,
    actor: EventActor,
    event_type: EventType,
    task_id: str | None = None,
    transition_id: str | None = None,
    job_id: str | None = None,
    data: Mapping[str, object] | None = None,
) -> EventEnvelope:
    return build_event(
        project_id=project_id,
        actor=actor,
        event_type=event_type,
        correlation_id=new_ulid(),
        task_id=task_id,
        transition_id=transition_id,
        job_id=job_id,
        data=data,
    )


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _job_to_dict(job: ExecutionJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "task_id": job.task_id,
        "transition_id": job.transition_id,
        "worker_id": job.worker_id,
        "workspace_path": job.workspace_path,
        "status": str(job.status.value if hasattr(job.status, "value") else job.status),
        "attempts": job.attempts,
        "metadata": dict(job.metadata),
    }
