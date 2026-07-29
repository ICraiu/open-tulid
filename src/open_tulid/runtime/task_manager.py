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
from .execution_contracts import (
    compile_task_execution_contract,
    execution_contract_to_dict,
)
from .task_contracts import (
    implementation_contract_required,
)


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
    job_id: str | None = None
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
        project_root: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.project_root = project_root
        self.repo_root = repo_root

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
        requirement_errors = _validate_task_state_requirements(task, transition.to_state, self.workflow)
        if requirement_errors:
            return CommandResult(accepted=False, errors=tuple(requirement_errors))
        events = (_event(
            project_id=command.project_id,
            actor=command.actor,
            event_type=EventType.TransitionAccepted,
            task_id=task.id,
            transition_id=transition.id,
            data={"from": transition.from_state, "to": transition.to_state},
        ), _event(
            project_id=command.project_id,
            actor=command.actor,
            event_type=EventType.TaskMoved,
            task_id=task.id,
            transition_id=transition.id,
            data={"from": transition.from_state, "to": transition.to_state},
        ))
        effects = ({
            "type": "move_task",
            "task_id": task.id,
            "from_state": transition.from_state,
            "to_state": transition.to_state,
        },)
        return CommandResult(accepted=True, events=events, effects=effects)

    def create_execution_job(self, command: CreateExecutionJob) -> CommandResult:
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
        if transition.worker is None:
            return CommandResult(accepted=False, errors=(_error(
                "transition.worker_missing",
                f"Transition {transition.id!r} has no worker.",
                transition.id,
            ),))
        frozen_contract = None
        if implementation_contract_required(task, self.workflow):
            if self.project_root is None:
                return CommandResult(accepted=False, errors=(_error(
                    "execution_contract.project_root_missing",
                    "Contract-backed execution requires a project tracker root.",
                    task.id,
                ),))
            compiled = compile_task_execution_contract(
                project_root=self.project_root,
                repo_root=self.repo_root,
                task=task,
                transition=transition,
            )
            if not compiled.accepted or compiled.contract is None:
                return CommandResult(accepted=False, errors=compiled.errors)
            frozen_contract = compiled.contract
        job_id = command.job_id or new_ulid()
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
                **(
                    {
                        "execution_contract": execution_contract_to_dict(frozen_contract),
                        "execution_contract_sha256": frozen_contract.sha256,
                    }
                    if frozen_contract is not None
                    else {}
                ),
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
            _event(
                project_id=command.project_id,
                actor=command.actor,
                event_type=EventType.ExecutionJobCreated,
                task_id=command.task_id,
                transition_id=command.transition_id,
                job_id=job.job_id,
                data={
                    "worker_id": job.worker_id,
                    "workspace_path": job.workspace_path,
                    **(
                        {"execution_contract_sha256": frozen_contract.sha256}
                        if frozen_contract is not None
                        else {}
                    ),
                },
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
        if task.task_type in workflow.task_types and task.current_state in workflow.states:
            errors.extend(_validate_task_state_requirements(task, task.current_state, workflow))
    return errors


def _validate_task_state_requirements(
    task,
    state: str,
    workflow: WorkflowDefinition,
) -> list[DomainError]:
    """Validate requirements observable from a persisted Task snapshot.

    Artifact links are the only requirement evidence currently carried by the
    task model. Validation calls and changed-file evidence are execution-time
    concepts, so this deliberately leaves them to the verifier.
    """
    task_type = workflow.task_types.get(task.task_type)
    if task_type is None:
        return []
    requirements = task_type.requirements_by_state.get(state)
    if requirements is None:
        return []
    errors: list[DomainError] = []
    for artifact_type in requirements.artifacts:
        if not _has_artifact_link(task.artifact_links, artifact_type):
            errors.append(_error(
                "task.required_artifact_missing",
                f"Task {task.id!r} in state {state!r} requires artifact {artifact_type!r}.",
                task.id,
            ))
    return errors


def _has_artifact_link(links: tuple[str, ...], artifact_type: str) -> bool:
    # Completion-promoted links are shaped like artifacts/<task>/<type>/<file>.
    # Accept bare type links too, since adapters may preserve user-authored links.
    return any(artifact_type in Path(link).parts or link == artifact_type for link in links)


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
