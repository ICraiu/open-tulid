from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.domain import (
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkflowDefinition,
)
from open_tulid.runtime import CompletionService, CompletionSubmission, FileExecutionJobStore, JsonlEventStore


TASK_ID = "01J00000000000000000000001"


@dataclass
class FakeAdapter:
    task: Task
    moved_to: str | None = None
    name: str = "fake"
    capabilities: frozenset[AdapterCapability] = frozenset({
        AdapterCapability.LOAD_PROJECT,
        AdapterCapability.READ_TASK,
        AdapterCapability.MOVE_TASK,
    })

    def load_project(self) -> LoadProjectResult:
        return LoadProjectResult(snapshot=ProjectSnapshot(
            project_id="Agent",
            tasks=MappingProxyType({self.task.id: self.task}),
            board_positions=MappingProxyType({}),
        ))

    def read_task(self, task_id: str) -> ReadTaskResult:
        return ReadTaskResult(task=self.task) if task_id == self.task.id else ReadTaskResult()

    def write_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        self.moved_to = state
        return WriteResult(path=state)

    def append_event(self, event: Mapping[str, Any]) -> WriteResult:
        return WriteResult(path="events/test.jsonl")


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "CodeReview": StateDefinition(id="CodeReview"),
        }),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex",
                requires=RequirementDefinition(artifacts=("result.md",)),
                transaction=None,
            ),
        }),
    )


def _task() -> Task:
    return Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
    )


def _job_store(tmp_path: Path) -> FileExecutionJobStore:
    store = FileExecutionJobStore(tmp_path / "jobs")
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    workspace.mkdir()
    output.mkdir()
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret", "output_path": str(output)},
    )).accepted is True
    return store


def test_completion_rejects_wrong_token(tmp_path: Path):
    store = _job_store(tmp_path)
    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="wrong",
        submission=CompletionSubmission(),
    )

    assert result.accepted is False
    assert result.errors[0].code == "completion.identity_mismatch"


def test_completion_rejects_missing_required_artifact(tmp_path: Path):
    store = _job_store(tmp_path)
    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(summary="done"),
    )

    assert result.accepted is False
    assert result.errors[0].code == "completion.artifact_missing"


def test_completion_accepts_evidence_and_moves_task(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    adapter = FakeAdapter(_task())
    service = CompletionService(
        workflow=_workflow(),
        adapter=adapter,
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(summary="done", artifacts=("result.md",)),
    )

    assert result.accepted is True
    assert adapter.moved_to == "CodeReview"
    loaded = store.get("01J00000000000000000000JOB")
    assert loaded.job is not None
    assert loaded.job.status == "accepted"
    assert [event.event_type for event in JsonlEventStore(tmp_path / "events").iter_events()][-2:] == [
        "ReviewRequested",
        "ExecutionFinished",
    ]
