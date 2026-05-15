from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import DomainError, EventEnvelope, ExecutionJob, ProjectSnapshot, Task, TransitionDefinition, WorkflowDefinition

from .events import new_ulid
from .events import JsonlEventStore, TransactionJournalStore
from .jobs import FileExecutionJobStore
from .resources import FileResourceLeaseStore
from .task_manager import CreateExecutionJob, TaskManager
from .transactions import FileTransactionRuntime


@dataclass(frozen=True)
class ScheduleResult:
    scheduled: bool
    job: ExecutionJob | None = None
    task_id: str | None = None
    transition_id: str | None = None
    errors: tuple[DomainError, ...] = ()
    skipped: tuple[DomainError, ...] = ()
    events: tuple[EventEnvelope, ...] = ()
    events_persisted: bool = False

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
        lease_store: FileResourceLeaseStore | None = None,
        worker_resources: dict[str, tuple[str, ...]] | None = None,
        event_store: JsonlEventStore | None = None,
        journal_store: TransactionJournalStore | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.workspace_root = workspace_root
        self.lease_store = lease_store
        self.worker_resources = worker_resources or {}
        self.event_store = event_store
        self.journal_store = journal_store

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

            required_resources = self.worker_resources.get(transition.worker or "", ())
            reserved_job_id = new_ulid()
            manager = TaskManager(
                workflow=self.workflow,
                adapter=self.adapter,
                job_store=None if self._transactional_creation_enabled else self.job_store,
            )
            create_command = CreateExecutionJob(
                project_id=project_id,
                task_id=task.id,
                transition_id=transition.id,
                workspace_root=self.workspace_root,
                job_id=reserved_job_id,
            )
            if required_resources and self.lease_store is not None:
                reserved, created = self.lease_store.admit(
                    required_resources,
                    job_id=reserved_job_id,
                    worker_id=transition.worker or "",
                    owner_path=self.job_store.path_for(reserved_job_id),
                    commit=lambda: self._create_job(manager, create_command),
                    accepted=lambda result: result.accepted,
                )
                if not reserved.acquired:
                    skipped.append(_error(
                        "resource.busy",
                        f"Task {task.id!r} requires busy resources: {', '.join(reserved.busy_resources)}.",
                        task.id,
                    ))
                    continue
                assert created is not None
            else:
                created = self._create_job(manager, create_command)
            if not created.accepted:
                if required_resources and self.lease_store is not None:
                    self.lease_store.release_job(reserved_job_id)
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
                events_persisted=self._transactional_creation_enabled,
            )

        return ScheduleResult(scheduled=False, skipped=tuple(skipped))

    @property
    def _transactional_creation_enabled(self) -> bool:
        return self.event_store is not None and self.journal_store is not None

    def _create_job(self, manager: TaskManager, command: CreateExecutionJob):
        created = manager.handle(command)
        if not created.accepted or not self._transactional_creation_enabled:
            return created
        runtime = FileTransactionRuntime(
            journals=self.journal_store,
            events=self.event_store,
            apply_effect=self._apply_job_effect,
        )
        applied = runtime.apply(
            project_id=command.project_id,
            effects=created.effects,
            events=created.events,
            task_id=command.task_id,
            transition_id=command.transition_id,
        )
        if applied.accepted:
            return created
        return type(created)(
            accepted=False,
            errors=(applied.error or _error("transaction.failed", "Execution job transaction failed."),),
            events=created.events,
            effects=created.effects,
            job=created.job,
        )

    def _apply_job_effect(self, effect):
        if effect.get("type") != "create_execution_job":
            return _EffectResult(False, "unsupported job effect", (_error("effect.unsupported", "Unsupported job effect."),))
        payload = effect.get("job")
        if not isinstance(payload, dict):
            return _EffectResult(False, "job payload missing", (_error("job.payload_invalid", "Job payload is invalid."),))
        try:
            saved = self.job_store.create(ExecutionJob(**payload))
        except (TypeError, ValueError) as exc:
            return _EffectResult(False, str(exc), (_error("job.payload_invalid", str(exc)),))
        if not saved.accepted:
            return _EffectResult(False, saved.error.message if saved.error else "job create failed", (saved.error,) if saved.error else ())
        return _EffectResult(True)


@dataclass(frozen=True)
class _EffectResult:
    accepted: bool
    message: str = ""
    errors: tuple[DomainError, ...] = ()


def recover_job_creation_transactions(
    *,
    job_store: FileExecutionJobStore,
    event_store: JsonlEventStore,
    journal_store: TransactionJournalStore,
) -> tuple[str, ...]:
    recovered: list[str] = []
    existing_event_ids = {event.event_id for event in event_store.iter_events()}
    for record in journal_store.list_incomplete():
        effects = tuple(effect for effect in record.effects if effect.get("type") == "create_execution_job")
        if len(effects) != 1:
            continue
        payload = effects[0].get("job")
        if not isinstance(payload, dict):
            continue
        job_id = payload.get("job_id")
        if not isinstance(job_id, str):
            continue
        loaded = job_store.get(job_id)
        if not loaded.accepted:
            try:
                saved = job_store.create(ExecutionJob(**payload))
            except (TypeError, ValueError):
                continue
            if not saved.accepted:
                continue
        missing_events = tuple(event for event in record.events if event.event_id not in existing_event_ids)
        if missing_events:
            appended = event_store.append_many(missing_events)
            if not appended.accepted:
                continue
            existing_event_ids.update(event.event_id for event in missing_events)
        committed = journal_store.commit(record)
        if committed.accepted:
            recovered.append(record.journal_id)
    return tuple(recovered)


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
