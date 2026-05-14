from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import DomainError, EventEnvelope, ExecutionJob, ProjectSnapshot, Task, TransitionDefinition, WorkflowDefinition

from .jobs import FileExecutionJobStore
from .task_manager import CreateExecutionJob, TaskManager


@dataclass(frozen=True)
class ScheduleResult:
    scheduled: bool
    job: ExecutionJob | None = None
    task_id: str | None = None
    transition_id: str | None = None
    errors: tuple[DomainError, ...] = ()
    skipped: tuple[DomainError, ...] = ()
    events: tuple[EventEnvelope, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


class Scheduler:
    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        adapter: StorageAdapter,
        job_store: FileExecutionJobStore,
        workspace_root: Path,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.workspace_root = workspace_root

    def schedule_one(self, project_id: str) -> ScheduleResult:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return ScheduleResult(scheduled=False, errors=loaded.errors)
        if loaded.snapshot is None:
            return ScheduleResult(scheduled=False, errors=(_error(
                "project.snapshot_missing",
                "Adapter returned no project snapshot.",
            ),))

        skipped: list[DomainError] = []
        for task in _tasks_in_board_order(loaded.snapshot):
            dependency_error = _dependency_error(task, loaded.snapshot, self.workflow)
            if dependency_error is not None:
                skipped.append(dependency_error)
                continue

            transition_result = _select_transition(task, self.workflow)
            if isinstance(transition_result, DomainError):
                skipped.append(transition_result)
                continue
            transition = transition_result

            active = self.job_store.find_active(project_id, task.id, transition.id)
            if not active.accepted:
                return ScheduleResult(
                    scheduled=False,
                    errors=(active.error or _error("job.read_failed", "Cannot inspect active jobs."),),
                    skipped=tuple(skipped),
                )
            if active.jobs:
                skipped.append(_error(
                    "job.active_exists",
                    f"Task {task.id!r} already has an active job for transition {transition.id!r}.",
                    task.id,
                ))
                continue

            manager = TaskManager(
                workflow=self.workflow,
                adapter=self.adapter,
                job_store=self.job_store,
            )
            created = manager.handle(CreateExecutionJob(
                project_id=project_id,
                task_id=task.id,
                transition_id=transition.id,
                workspace_root=self.workspace_root,
            ))
            if not created.accepted:
                return ScheduleResult(
                scheduled=False,
                task_id=task.id,
                transition_id=transition.id,
                errors=created.errors,
                skipped=tuple(skipped),
                events=created.events,
            )
            return ScheduleResult(
                scheduled=True,
                job=created.job,
                task_id=task.id,
                transition_id=transition.id,
                skipped=tuple(skipped),
                events=created.events,
            )

        return ScheduleResult(scheduled=False, skipped=tuple(skipped))


def _tasks_in_board_order(snapshot: ProjectSnapshot) -> tuple[Task, ...]:
    return tuple(sorted(
        snapshot.tasks.values(),
        key=lambda task: (
            snapshot.board_positions.get(task.id).board if task.id in snapshot.board_positions else "",
            snapshot.board_positions.get(task.id).line if task.id in snapshot.board_positions else 10**9,
            task.id,
        ),
    ))


def _dependency_error(
    task: Task,
    snapshot: ProjectSnapshot,
    workflow: WorkflowDefinition,
) -> DomainError | None:
    for dependency_id in task.dependencies:
        dependency = snapshot.tasks.get(dependency_id)
        if dependency is None:
            return _error(
                "task.dependency_missing",
                f"Task {task.id!r} depends on missing task {dependency_id!r}.",
                task.id,
            )
        if _has_outgoing_transition(dependency, workflow):
            return _error(
                "task.dependency_unmet",
                f"Task {task.id!r} depends on unfinished task {dependency_id!r}.",
                task.id,
            )
    return None


def _has_outgoing_transition(task: Task, workflow: WorkflowDefinition) -> bool:
    return any(
        transition.task_type == task.task_type and transition.from_state == task.current_state
        for transition in workflow.transitions.values()
    )


def _select_transition(task: Task, workflow: WorkflowDefinition) -> TransitionDefinition | DomainError:
    transitions = tuple(
        transition for transition in workflow.transitions.values()
        if transition.task_type == task.task_type
        and transition.from_state == task.current_state
        and transition.worker is not None
    )
    if not transitions:
        return _error(
            "scheduler.no_transition",
            f"Task {task.id!r} has no scheduler-eligible transition from state {task.current_state!r}.",
            task.id,
        )
    if len(transitions) == 1:
        return transitions[0]
    defaults = tuple(t for t in transitions if t.default_for_scheduler)
    if len(defaults) == 1:
        return defaults[0]
    return _error(
        "scheduler.ambiguous_transition",
        f"Task {task.id!r} has multiple scheduler-eligible transitions and no single default.",
        task.id,
    )


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
