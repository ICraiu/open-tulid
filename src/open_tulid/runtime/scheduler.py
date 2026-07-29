from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import fcntl

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import (
    DomainError,
    EventActor,
    EventEnvelope,
    EventType,
    ExecutionJob,
    ExecutionJobStatus,
    ProjectSnapshot,
    Task,
    TransitionDefinition,
    WorkflowDefinition,
)

from .events import build_event, new_ulid
from .events import JsonlEventStore, TransactionJournalStore
from .jobs import FileExecutionJobStore, JobStoreResult
from .resources import FileResourceLeaseStore
from .task_contracts import (
    implementation_contract_required,
    validate_task_implementation_contract,
)
from .task_manager import CreateExecutionJob, TaskManager
from .transactions import FileTransactionRuntime


RECENT_FAILURE_BACKOFF_SECONDS = 60


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
        serial_repo_execution: bool = True,
        failed_job_backoff_seconds: int = RECENT_FAILURE_BACKOFF_SECONDS,
        max_failed_attempts_per_transition: int = 0,
        runtime_session_started_at: datetime | None = None,
        event_store: JsonlEventStore | None = None,
        journal_store: TransactionJournalStore | None = None,
        project_root: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.workspace_root = workspace_root
        self.lease_store = lease_store
        self.worker_resources = worker_resources or {}
        self.serial_repo_execution = serial_repo_execution
        self.failed_job_backoff_seconds = failed_job_backoff_seconds
        self.max_failed_attempts_per_transition = max_failed_attempts_per_transition
        self.runtime_session_started_at = (
            runtime_session_started_at.astimezone(timezone.utc)
            if runtime_session_started_at is not None
            else None
        )
        self.event_store = event_store
        self.journal_store = journal_store
        self.project_root = project_root
        self.repo_root = repo_root

    def schedule_one(self, project_id: str) -> ScheduleResult:
        with self._locked():
            return self._schedule_one_locked(project_id)

    def _schedule_one_locked(self, project_id: str) -> ScheduleResult:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return ScheduleResult(scheduled=False, errors=loaded.errors)
        if loaded.snapshot is None:
            return ScheduleResult(scheduled=False, errors=(_error(
                "project.snapshot_missing",
                "Adapter returned no project snapshot.",
            ),))

        skipped: list[DomainError] = []
        tasks = _tasks_in_board_order(loaded.snapshot)
        if self.serial_repo_execution:
            focused = _serial_repo_lane_focus(
                project_id,
                loaded.snapshot,
                self.workflow,
                self.job_store,
            )
            if isinstance(focused, DomainError):
                return ScheduleResult(scheduled=False, errors=(focused,))
            if focused is not None:
                if focused.active_jobs:
                    repair_job = _repair_ready_job(focused.active_jobs)
                    if repair_job is not None:
                        return ScheduleResult(
                            scheduled=True,
                            job=repair_job,
                            task_id=repair_job.task_id,
                            transition_id=repair_job.transition_id,
                        )
                    skipped.append(_error(
                        "repo_lane.active_job_exists",
                        (
                            f"Repo lane is held by task {focused.task.id!r}; "
                            f"active job {focused.active_jobs[0].job_id!r} is still running."
                        ),
                        focused.task.id,
                    ))
                    return ScheduleResult(
                        scheduled=False,
                        task_id=focused.task.id,
                        skipped=tuple(skipped),
                    )
                tasks = (focused.task,)

        for task in tasks:
            dependency_error = _dependency_error(task, loaded.snapshot, self.workflow)
            if dependency_error is not None:
                skipped.append(dependency_error)
                continue

            if (
                self.project_root is not None
                and implementation_contract_required(task, self.workflow)
            ):
                contract = validate_task_implementation_contract(self.project_root, task)
                if not contract.accepted:
                    recovery = _contract_recovery_transition(task, self.workflow)
                    if isinstance(recovery, DomainError):
                        skipped.append(recovery)
                        continue
                    moved = self.adapter.move_task(task.id, recovery.to_state)
                    if not moved.accepted:
                        return ScheduleResult(
                            scheduled=False,
                            task_id=task.id,
                            errors=moved.errors or (_error(
                                "contract.invalidation_move_failed",
                                f"Could not move task {task.id!r} back to {recovery.to_state!r}.",
                                task.id,
                            ),),
                            skipped=tuple(skipped),
                        )
                    event = build_event(
                        project_id=project_id,
                        actor=EventActor(type="system", id="contract-readiness-guard"),
                        event_type=EventType.ContractInvalidated,
                        correlation_id=new_ulid(),
                        task_id=task.id,
                        transition_id=recovery.id,
                        data={
                            "from_state": task.current_state,
                            "to_state": recovery.to_state,
                            "error_codes": [error.code for error in contract.errors],
                            "reason": "generated_contract_missing_or_stale",
                        },
                    )
                    events_persisted = False
                    if self.event_store is not None:
                        appended = self.event_store.append(event)
                        if not appended.accepted:
                            return ScheduleResult(
                                scheduled=False,
                                task_id=task.id,
                                errors=(appended.error or _error(
                                    "event.append_failed",
                                    "Could not record contract invalidation.",
                                    task.id,
                                ),),
                                skipped=tuple(skipped),
                            )
                        events_persisted = True
                    invalidated = _error(
                        "task.contract_invalidated",
                        (
                            f"Task {task.id!r} changed after its execution contract was "
                            f"prepared; moved it to {recovery.to_state!r} for regeneration."
                        ),
                        task.id,
                    )
                    return ScheduleResult(
                        scheduled=False,
                        task_id=task.id,
                        transition_id=recovery.id,
                        skipped=(*skipped, invalidated),
                        events=(event,),
                        events_persisted=events_persisted,
                    )

            transition_result = select_scheduler_transition(task, self.workflow)
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
                repair_job = _repair_ready_job(active.jobs)
                if repair_job is not None:
                    return ScheduleResult(
                        scheduled=True,
                        job=repair_job,
                        task_id=repair_job.task_id,
                        transition_id=repair_job.transition_id,
                        skipped=tuple(skipped),
                    )
                skipped.append(_error(
                    "job.active_exists",
                    f"Task {task.id!r} already has an active job for transition {transition.id!r}.",
                    task.id,
                ))
                continue

            if self.max_failed_attempts_per_transition > 0:
                failed_attempts = _find_failed_jobs(
                    self.job_store,
                    project_id,
                    task.id,
                    transition.id,
                    since=self.runtime_session_started_at,
                )
                if not failed_attempts.accepted:
                    return ScheduleResult(
                        scheduled=False,
                        errors=(failed_attempts.error or _error("job.read_failed", "Cannot inspect failed jobs."),),
                        skipped=tuple(skipped),
                    )
                if len(failed_attempts.jobs) >= self.max_failed_attempts_per_transition:
                    skipped.append(_error(
                        "job.retry_limit_reached",
                        (
                            f"Task {task.id!r} failed transition {transition.id!r} "
                            f"{len(failed_attempts.jobs)} time(s); retry limit "
                            f"{self.max_failed_attempts_per_transition} reached."
                        ),
                        task.id,
                    ))
                    continue

            recent_failure = _find_recent_failed_job(
                self.job_store,
                project_id,
                task.id,
                transition.id,
                since=self.runtime_session_started_at,
                backoff_seconds=self.failed_job_backoff_seconds,
            )
            if not recent_failure.accepted:
                return ScheduleResult(
                    scheduled=False,
                    errors=(recent_failure.error or _error("job.read_failed", "Cannot inspect recent jobs."),),
                    skipped=tuple(skipped),
                )
            if recent_failure.jobs:
                skipped.append(_error(
                    "job.recent_failure",
                    (
                        f"Task {task.id!r} recently failed transition {transition.id!r}; "
                        f"waiting {self.failed_job_backoff_seconds}s before retry."
                    ),
                    task.id,
                ))
                continue

            required_resources = self.worker_resources.get(transition.worker or "", ())
            reserved_job_id = new_ulid()
            manager = TaskManager(
                workflow=self.workflow,
                adapter=self.adapter,
                job_store=None if self._transactional_creation_enabled else self.job_store,
                project_root=self.project_root,
                repo_root=self.repo_root,
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

    @contextmanager
    def _locked(self):
        lock_path = self.job_store.root.parent / ".scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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


@dataclass(frozen=True)
class _SerialRepoLaneFocus:
    task: Task
    active_jobs: tuple[ExecutionJob, ...] = ()


def _serial_repo_lane_focus(
    project_id: str,
    snapshot: ProjectSnapshot,
    workflow: WorkflowDefinition,
    job_store: FileExecutionJobStore,
) -> _SerialRepoLaneFocus | DomainError | None:
    listed = job_store.list()
    if not listed.accepted:
        return listed.error or _error("job.read_failed", "Cannot inspect execution jobs.")

    tasks = _tasks_in_board_order(snapshot)
    task_ids = {task.id for task in tasks}
    project_jobs = tuple(
        job for job in listed.jobs
        if job.project_id == project_id and job.task_id in task_ids
    )
    active_jobs_by_task: dict[str, list[ExecutionJob]] = {}
    jobs_by_task: dict[str, list[ExecutionJob]] = {}
    for job in project_jobs:
        jobs_by_task.setdefault(job.task_id, []).append(job)
        if _status_value(job.status) in _ACTIVE_REPO_LANE_JOB_STATUSES:
            active_jobs_by_task.setdefault(job.task_id, []).append(job)

    for task in tasks:
        active_jobs = tuple(active_jobs_by_task.get(task.id, ()))
        if active_jobs:
            return _SerialRepoLaneFocus(task=task, active_jobs=active_jobs)

    for task in tasks:
        task_jobs = jobs_by_task.get(task.id, ())
        if (
            _has_accepted_job(task_jobs)
            and _has_scheduler_eligible_transition(task, workflow)
        ):
            return _SerialRepoLaneFocus(task=task)
    return None


def _repair_ready_job(jobs: tuple[ExecutionJob, ...]) -> ExecutionJob | None:
    """A rejected implementation job resumes in-place; it is never recreated."""
    ready = [
        job for job in jobs
        if _status_value(job.status) == ExecutionJobStatus.COMPLETION_REJECTED.value
        and job.metadata.get("repair_ready") is True
    ]
    return max(ready, key=_job_timestamp) if ready else None


def _find_recent_failed_job(
    job_store: FileExecutionJobStore,
    project_id: str,
    task_id: str,
    transition_id: str,
    *,
    now: datetime | None = None,
    since: datetime | None = None,
    backoff_seconds: int = RECENT_FAILURE_BACKOFF_SECONDS,
) -> JobStoreResult:
    listed = job_store.list()
    if not listed.accepted:
        return listed
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=backoff_seconds)
    session_cutoff = since.astimezone(timezone.utc) if since is not None else None
    recent = tuple(
        job for job in listed.jobs
        if job.project_id == project_id
        and job.task_id == task_id
        and job.transition_id == transition_id
        and _status_value(job.status) == ExecutionJobStatus.FAILED.value
        and (updated_at := _job_timestamp(job)) is not None
        and updated_at >= cutoff
        and (session_cutoff is None or updated_at >= session_cutoff)
    )
    if not recent:
        return JobStoreResult(jobs=())
    return JobStoreResult(jobs=(max(
        recent,
        key=lambda job: _job_timestamp(job) or datetime.min.replace(tzinfo=timezone.utc),
    ),))


def _find_failed_jobs(
    job_store: FileExecutionJobStore,
    project_id: str,
    task_id: str,
    transition_id: str,
    *,
    since: datetime | None = None,
) -> JobStoreResult:
    listed = job_store.list()
    if not listed.accepted:
        return listed
    session_cutoff = since.astimezone(timezone.utc) if since is not None else None
    failed = tuple(
        job for job in listed.jobs
        if job.project_id == project_id
        and job.task_id == task_id
        and job.transition_id == transition_id
        and _status_value(job.status) == ExecutionJobStatus.FAILED.value
        and (
            session_cutoff is None
            or ((updated_at := _job_timestamp(job)) is not None and updated_at >= session_cutoff)
        )
    )
    return JobStoreResult(jobs=failed)


def _job_timestamp(job: ExecutionJob) -> datetime | None:
    for key in ("updated_at", "created_at"):
        raw = job.metadata.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _status_value(status: ExecutionJobStatus | str) -> str:
    return status.value if hasattr(status, "value") else str(status)


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


def _has_accepted_job(jobs: list[ExecutionJob]) -> bool:
    return any(
        _status_value(job.status) == ExecutionJobStatus.ACCEPTED.value
        for job in jobs
    )


_ACTIVE_REPO_LANE_JOB_STATUSES = frozenset({
    ExecutionJobStatus.PENDING.value,
    ExecutionJobStatus.RUNNING.value,
    ExecutionJobStatus.COMPLETION_SUBMITTED.value,
    ExecutionJobStatus.COMPLETION_REJECTED.value,
    ExecutionJobStatus.STALE.value,
})


def _has_scheduler_eligible_transition(task: Task, workflow: WorkflowDefinition) -> bool:
    return any(
        transition.task_type == task.task_type
        and transition.from_state == task.current_state
        and transition.worker is not None
        for transition in workflow.transitions.values()
    )


def _contract_recovery_transition(
    task: Task,
    workflow: WorkflowDefinition,
) -> TransitionDefinition | DomainError:
    preparation_sources = {
        transition.from_state
        for transition in workflow.transitions.values()
        if transition.task_type == task.task_type
        and "ImplementationContract" in transition.requires.artifacts
    }
    candidates = tuple(
        transition
        for transition in workflow.transitions.values()
        if transition.task_type == task.task_type
        and transition.from_state == task.current_state
        and transition.to_state in preparation_sources
        and transition.worker is None
    )
    if len(candidates) == 1:
        return candidates[0]
    return _error(
        "contract.invalidation_transition_missing",
        (
            f"Task {task.id!r} has a missing or stale ImplementationContract, but "
            f"workflow state {task.current_state!r} has no single workerless recovery "
            "transition back to contract preparation."
        ),
        task.id,
    )


def select_scheduler_transition(task: Task, workflow: WorkflowDefinition) -> TransitionDefinition | DomainError:
    """Select the worker-backed transition the scheduler would use for a task."""
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
