from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.domain import (
    BoardPosition,
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkflowDefinition,
)
from open_tulid.runtime import FileExecutionJobStore, Scheduler


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


def _workflow(*, ambiguous: bool = False) -> WorkflowDefinition:
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
    assert [event.event_type for event in result.events] == ["TransitionAccepted", "ExecutionJobCreated"]
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
    assert result.skipped[0].code == "job.active_exists"
