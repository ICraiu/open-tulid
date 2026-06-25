from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from multiprocessing import get_context
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.domain import (
    BoardPosition,
    DomainError,
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkflowDefinition,
)
from open_tulid.models import ResourceConfig
from open_tulid.runtime import (
    FileExecutionJobStore,
    FileResourceLeaseStore,
    JsonlEventStore,
    Scheduler,
    TransactionJournalStore,
    recover_job_creation_transactions,
)


TASK_ID = "01J00000000000000000000001"


@dataclass
class FakeAdapter:
    snapshot: ProjectSnapshot
    name: str = "fake"
    capabilities: frozenset[AdapterCapability] = frozenset({AdapterCapability.LOAD_PROJECT})

    def load_project(self) -> LoadProjectResult:
        return LoadProjectResult(snapshot=self.snapshot)

    def read_task(self, task_id: str) -> ReadTaskResult:
        task = self.snapshot.tasks.get(task_id)
        return ReadTaskResult(task=task) if task else ReadTaskResult()

    def write_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        return WriteResult(path=state)

    def append_event(self, event: Mapping[str, Any]) -> WriteResult:
        return WriteResult(path="events/test.jsonl")


def _workflow(*, ambiguous: bool = False, review: bool = False) -> WorkflowDefinition:
    transitions = {
        "implement": TransitionDefinition(
            id="implement",
            task_type="task",
            from_state="Todo",
            to_state="Review",
            worker="codex",
            requires=RequirementDefinition(),
            transaction=None,
            default_for_scheduler=True,
        ),
    }
    if review:
        transitions["review"] = TransitionDefinition(
            id="review",
            task_type="task",
            from_state="Review",
            to_state="Done",
            worker="codex",
            requires=RequirementDefinition(),
            transaction=None,
            default_for_scheduler=True,
        )
    if ambiguous:
        transitions["document"] = TransitionDefinition(
            id="document",
            task_type="task",
            from_state="Todo",
            to_state="Review",
            worker="codex",
            requires=RequirementDefinition(),
            transaction=None,
            default_for_scheduler=True,
        )
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "Review": StateDefinition(id="Review"),
            "Done": StateDefinition(id="Done"),
        }),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType(transitions),
    )


def _snapshot(*tasks: Task) -> ProjectSnapshot:
    if not tasks:
        tasks = (Task(
            id=TASK_ID,
            title="Implement thing",
            path="tasks/thing.md",
            current_state="Todo",
            task_type="task",
        ),)
    return ProjectSnapshot(
        project_id="Agent",
        tasks=MappingProxyType({task.id: task for task in tasks}),
        board_positions=MappingProxyType({
            task.id: BoardPosition(board="Work", column=task.current_state, card_text=task.title, line=index)
            for index, task in enumerate(tasks, start=1)
        }),
    )


def test_file_execution_job_store_persists_one_job_json(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    job = ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
    )

    created = store.create(job)
    loaded = store.get(job.job_id)

    assert created.accepted is True
    assert loaded.accepted is True
    assert loaded.job is not None
    assert loaded.job.task_id == TASK_ID
    assert (tmp_path / "jobs" / job.job_id / "job.json").is_file()


def test_file_execution_job_store_rejects_duplicate_active_job(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    job = ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
    )

    first = store.create(job)
    duplicate = store.create(ExecutionJob(
        job_id="01J00000000000000000000J02",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work2"),
    ))

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.error is not None
    assert duplicate.error.code == "job.active_exists"


def test_file_execution_job_store_treats_stale_job_as_reschedule_blocker(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    stale = ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="stale",
    )

    assert store.create(stale).accepted is True
    duplicate = store.create(ExecutionJob(
        job_id="01J00000000000000000000J02",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work2"),
    ))

    assert duplicate.accepted is False
    assert duplicate.error is not None
    assert duplicate.error.code == "job.active_exists"


def _create_same_active_job(root: str, suffix: str, queue) -> None:
    store = FileExecutionJobStore(Path(root))
    result = store.create(ExecutionJob(
        job_id=f"01J0000000000000000000{suffix}",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(Path(root) / suffix),
    ))
    queue.put(result.accepted)


def test_file_execution_job_store_rejects_duplicate_active_job_across_processes(tmp_path: Path):
    ctx = get_context("fork")
    queue = ctx.Queue()
    root = tmp_path / "jobs"
    first = ctx.Process(target=_create_same_active_job, args=(str(root), "JOB", queue))
    second = ctx.Process(target=_create_same_active_job, args=(str(root), "J02", queue))

    first.start()
    second.start()
    first.join()
    second.join()

    assert sorted((queue.get(), queue.get())) == [False, True]


def test_scheduler_creates_first_runnable_job_in_board_order(tmp_path: Path):
    blocked = Task(
        id="01J00000000000000000000002",
        title="Blocked",
        path="tasks/blocked.md",
        current_state="Todo",
        task_type="task",
        dependencies=("missing",),
    )
    runnable = Task(
        id=TASK_ID,
        title="Runnable",
        path="tasks/runnable.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot(blocked, runnable)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == TASK_ID
    assert result.transition_id == "implement"
    assert result.job is not None
    assert [event.event_type for event in result.events] == ["ExecutionJobCreated"]
    assert store.get(result.job.job_id).accepted is True
    assert [skip.code for skip in result.skipped] == ["task.dependency_missing"]


def test_scheduler_skips_when_active_job_exists(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    existing = ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
    )
    assert store.create(existing).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "repo_lane.active_job_exists"


def test_scheduler_backs_off_after_recent_failed_job(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": datetime.now(timezone.utc).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.recent_failure"


def test_scheduler_uses_configured_failed_job_backoff(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=1,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True


def test_scheduler_can_stop_after_configured_failed_attempts(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    for index in range(2):
        assert store.create(ExecutionJob(
            job_id=f"01J00000000000000000000J{index:02d}",
            project_id="Agent",
            task_id=TASK_ID,
            transition_id="implement",
            worker_id="codex",
            workspace_path=str(tmp_path / f"work-{index}"),
            status="failed",
            metadata={"updated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()},
        )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        max_failed_attempts_per_transition=2,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.retry_limit_reached"


def test_scheduler_ignores_retry_limit_failures_before_runtime_session(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    session_started_at = datetime.now(timezone.utc)
    for index in range(2):
        assert store.create(ExecutionJob(
            job_id=f"01J00000000000000000000J{index:02d}",
            project_id="Agent",
            task_id=TASK_ID,
            transition_id="implement",
            worker_id="codex",
            workspace_path=str(tmp_path / f"work-{index}"),
            status="failed",
            metadata={"updated_at": (session_started_at - timedelta(seconds=1)).isoformat()},
        )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        max_failed_attempts_per_transition=2,
        runtime_session_started_at=session_started_at,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.job is not None


def test_scheduler_counts_retry_limit_failures_in_runtime_session(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    session_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    for index in range(2):
        assert store.create(ExecutionJob(
            job_id=f"01J00000000000000000000J{index:02d}",
            project_id="Agent",
            task_id=TASK_ID,
            transition_id="implement",
            worker_id="codex",
            workspace_path=str(tmp_path / f"work-{index}"),
            status="failed",
            metadata={"updated_at": (session_started_at + timedelta(seconds=index + 1)).isoformat()},
        )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        max_failed_attempts_per_transition=2,
        runtime_session_started_at=session_started_at,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.retry_limit_reached"


def test_scheduler_ignores_recent_failures_before_runtime_session(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    session_started_at = datetime.now(timezone.utc)
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": (session_started_at - timedelta(seconds=1)).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        runtime_session_started_at=session_started_at,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.job is not None


def test_scheduler_retries_after_failed_job_backoff_expires(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.job is not None
    assert result.job.job_id != "01J00000000000000000000JOB"


def test_scheduler_does_not_pin_serial_lane_on_recent_failed_job(tmp_path: Path):
    blocked = Task(
        id=TASK_ID,
        title="Blocked",
        path="tasks/blocked.md",
        current_state="Todo",
        task_type="task",
    )
    next_task = Task(
        id="01J00000000000000000000002",
        title="Next",
        path="tasks/next.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000OLD",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": datetime.now(timezone.utc).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot(blocked, next_task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == next_task.id
    assert [error.code for error in result.skipped] == ["job.recent_failure"]


def test_scheduler_does_not_pin_serial_lane_on_retry_limited_job(tmp_path: Path):
    blocked = Task(
        id=TASK_ID,
        title="Blocked",
        path="tasks/blocked.md",
        current_state="Todo",
        task_type="task",
    )
    next_task = Task(
        id="01J00000000000000000000002",
        title="Next",
        path="tasks/next.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000OLD",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot(blocked, next_task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        max_failed_attempts_per_transition=1,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == next_task.id
    assert [error.code for error in result.skipped] == ["job.retry_limit_reached"]


def test_scheduler_keeps_started_task_on_serial_repo_lane(tmp_path: Path):
    started = Task(
        id=TASK_ID,
        title="Started",
        path="tasks/started.md",
        current_state="Review",
        task_type="task",
    )
    next_task = Task(
        id="01J00000000000000000000002",
        title="Next",
        path="tasks/next.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="accepted",
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(review=True),
        adapter=FakeAdapter(_snapshot(started, next_task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == TASK_ID
    assert result.transition_id == "review"


def test_scheduler_does_not_advance_or_switch_tasks_while_serial_lane_has_active_job(tmp_path: Path):
    started = Task(
        id=TASK_ID,
        title="Started",
        path="tasks/started.md",
        current_state="Review",
        task_type="task",
    )
    next_task = Task(
        id="01J00000000000000000000002",
        title="Next",
        path="tasks/next.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="running",
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(review=True),
        adapter=FakeAdapter(_snapshot(started, next_task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.task_id == TASK_ID
    assert result.skipped[0].code == "repo_lane.active_job_exists"


def test_scheduler_can_opt_out_of_serial_repo_lane(tmp_path: Path):
    started = Task(
        id=TASK_ID,
        title="Started",
        path="tasks/started.md",
        current_state="Review",
        task_type="task",
    )
    next_task = Task(
        id="01J00000000000000000000002",
        title="Next",
        path="tasks/next.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="running",
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(review=True),
        adapter=FakeAdapter(_snapshot(started, next_task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        serial_repo_execution=False,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == TASK_ID
    assert result.transition_id == "review"


def test_scheduler_defers_task_when_required_resource_is_busy(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    leases = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )
    assert leases.try_acquire(("local-llm",), job_id="existing", worker_id="codex").acquired is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        lease_store=leases,
        worker_resources={"codex": ("local-llm",)},
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "resource.busy"
    assert store.list().jobs == ()


def test_scheduler_reserves_resource_for_created_job(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    leases = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        lease_store=leases,
        worker_resources={"codex": ("local-llm",)},
    )

    result = scheduler.schedule_one("Agent")

    assert result.scheduled is True
    assert result.job is not None
    assert leases.job_holds(("local-llm",), result.job.job_id) is True


def test_scheduler_transactionally_persists_creation_events(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        event_store=JsonlEventStore(tmp_path / "events"),
        journal_store=TransactionJournalStore(tmp_path / "events" / "journals"),
    )

    result = scheduler.schedule_one("Agent")

    assert result.scheduled is True
    assert result.events_persisted is True
    assert [event.event_type for event in JsonlEventStore(tmp_path / "events").iter_events()] == [
        "ExecutionJobCreated",
    ]
    journals = TransactionJournalStore(tmp_path / "events" / "journals").iter_journals()
    assert len(journals) == 1
    assert journals[0].status == "committed"


def test_recover_job_creation_transactions_finishes_prepared_creation(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    event_store = JsonlEventStore(tmp_path / "events")
    journal_store = TransactionJournalStore(tmp_path / "events" / "journals")
    planner = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
    )
    planned = planner.schedule_one("Agent")
    assert planned.job is not None
    assert store.get(planned.job.job_id).accepted is True
    prepared = journal_store.prepare(
        journal_id="01J00000000000000000000JRN",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        effects=planned.events and ({"type": "create_execution_job", "job": {
            "job_id": planned.job.job_id,
            "project_id": planned.job.project_id,
            "task_id": planned.job.task_id,
            "transition_id": planned.job.transition_id,
            "worker_id": planned.job.worker_id,
            "workspace_path": planned.job.workspace_path,
            "status": str(planned.job.status.value if hasattr(planned.job.status, "value") else planned.job.status),
            "attempts": planned.job.attempts,
            "metadata": dict(planned.job.metadata),
        }},),
        events=planned.events,
    )
    assert prepared.accepted is True

    recovered = recover_job_creation_transactions(
        job_store=store,
        event_store=event_store,
        journal_store=journal_store,
    )

    assert recovered == ("01J00000000000000000000JRN",)
    assert [event.event_type for event in event_store.iter_events()] == [
        "ExecutionJobCreated",
    ]


def test_recover_job_creation_transactions_ignores_failed_creation(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    event_store = JsonlEventStore(tmp_path / "events")
    journal_store = TransactionJournalStore(tmp_path / "events" / "journals")
    planner = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=FileExecutionJobStore(tmp_path / "planned-jobs"),
        workspace_root=tmp_path / "workspaces",
    )
    planned = planner.schedule_one("Agent")
    assert planned.job is not None
    prepared = journal_store.prepare(
        journal_id="01J00000000000000000000JRN",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        effects=({"type": "create_execution_job", "job": {
            "job_id": planned.job.job_id,
            "project_id": planned.job.project_id,
            "task_id": planned.job.task_id,
            "transition_id": planned.job.transition_id,
            "worker_id": planned.job.worker_id,
            "workspace_path": planned.job.workspace_path,
            "status": str(planned.job.status.value if hasattr(planned.job.status, "value") else planned.job.status),
            "attempts": planned.job.attempts,
            "metadata": dict(planned.job.metadata),
        }},),
        events=planned.events,
    )
    assert prepared.record is not None
    journal_store.fail(prepared.record, DomainError(code="effect.failed", message="boom"))

    recovered = recover_job_creation_transactions(
        job_store=store,
        event_store=event_store,
        journal_store=journal_store,
    )

    assert recovered == ()
    assert store.list().jobs == ()
