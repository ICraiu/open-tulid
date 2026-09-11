from __future__ import annotations

import threading
import time
from pathlib import Path
from types import MappingProxyType

from open_tulid.adapters.base import LoadProjectResult, WriteResult
from open_tulid.domain import (
    ArtifactTypeDefinition,
    DerivesDefinition,
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkflowDefinition,
)
from open_tulid.runtime import (
    ArtifactSubmission,
    CompletionService,
    CompletionSubmission,
    FileExecutionJobStore,
    JsonlEventStore,
)


TASK_ID = "01J00000000000000000000001"

_VALID = (
    "# Deliver behavior\n\n"
    "Implement the agreed API.\n\n"
    "## Why\nClients need it.\n\n"
    "## What\nExpose the behavior.\n\n"
    "## How\nUse the existing service.\n\n"
    "## Acceptance\n- The agreed behavior works.\n"
)


class FakeAdapter:
    def __init__(self, task: Task):
        self._tasks: dict[str, Task] = {task.id: task}
        self.created: list[Task] = []
        self.moved_parents: list[str] = []

    def seed(self, *tasks: Task) -> None:
        for task in tasks:
            self._tasks[task.id] = task

    def load_project(self) -> LoadProjectResult:
        return LoadProjectResult(snapshot=ProjectSnapshot(
            project_id="Agent", tasks=MappingProxyType(self._tasks), board_positions=MappingProxyType({}),
        ))

    def read_task(self, task_id: str):
        task = self._tasks.get(task_id)
        return type("R", (), {"accepted": task is not None, "task": task})()

    def write_task(self, task: Task) -> WriteResult:
        self._tasks[task.id] = task
        return WriteResult(path=task.path)

    def create_task(self, task: Task) -> WriteResult:
        self._tasks[task.id] = task
        self.created.append(task)
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        task = self._tasks.get(task_id)
        if task is not None:
            self._tasks[task_id] = Task(
                id=task.id, title=task.title, path=task.path, current_state=state,
                task_type=task.task_type, dependencies=task.dependencies,
                artifact_links=task.artifact_links, parent_id=task.parent_id,
                metadata=task.metadata, body=task.body,
            )
        self.moved_parents.append(task_id)
        return WriteResult(path=state)

    def append_event(self, event) -> WriteResult:
        return WriteResult(path="events/x.jsonl")


def _derived_workflow(*, task_type: str = "chunk"):
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({"Todo": StateDefinition(id="Todo"), "CodeReview": StateDefinition(id="CodeReview")}),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
            task_type: TaskTypeDefinition(id=task_type, requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({"child_task": ArtifactTypeDefinition(id="child_task")}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code", task_type="task", from_state="Todo", to_state="CodeReview",
                worker="codex", requires=RequirementDefinition(), transaction=None,
                derives=DerivesDefinition(task_type=task_type, state="Todo", artifact_type="child_task"),
            ),
        }),
    )


def _parent(task_id: str = TASK_ID) -> Task:
    return Task(id=task_id, title="Parent", path=f"tasks/{task_id}.md", current_state="Todo", task_type="task")


def _store(tmp_path: Path) -> FileExecutionJobStore:
    return FileExecutionJobStore(tmp_path / "jobs")


def _job(store, name: str, task_id: str, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    out = workspace / "output"
    out.mkdir(parents=True, exist_ok=True)
    assert store.create(ExecutionJob(
        job_id=name, project_id="Agent", task_id=task_id, transition_id="code",
        worker_id="codex", workspace_path=str(workspace),
        metadata={"completion_token": "secret", "output_path": str(out)},
    )).accepted


def _job_ws(tmp_path: Path, name: str) -> Path:
    ws = tmp_path / "workspaces" / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "output").mkdir(parents=True, exist_ok=True)
    return ws


def _submit(service, job_id: str, artifacts):
    return service.submit(
        job_id=job_id, token="secret",
        submission=CompletionSubmission(summary="decomposed", artifacts=artifacts),
    )


def _child(workdir: Path, name: str, *, local_id: str, body: str = _VALID, deps=()):
    deps_line = "dependencies: [" + ",".join(f'"{d}"' for d in deps) + "]" if deps else ""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / name).write_text(
        f"---\nlocal_id: {local_id}\n{deps_line}\n---\n" + body,
        encoding="utf-8",
    )


def test_batch_rejects_invalid_artifact_path_leaves_zero_partial(tmp_path: Path):
    store = _store(tmp_path)
    workspace = _job_ws(tmp_path, "job")
    out = workspace / "output"
    _child(out, "good.md", local_id="good")
    _job(store, "01J00000000000000000000JOB", TASK_ID, workspace)
    adapter = FakeAdapter(_parent())
    service = CompletionService(workflow=_derived_workflow(), adapter=adapter, job_store=store,
                                event_store=JsonlEventStore(tmp_path / "events"))

    result = _submit(service, "01J00000000000000000000JOB", (
        ArtifactSubmission(type="child_task", path="good.md"),
        ArtifactSubmission(type="child_task", path="../escape.md"),
    ))

    assert result.accepted is False
    assert {error.code for error in result.errors} >= {"completion.artifact_outside_output"}
    assert adapter.created == []
    assert adapter.moved_parents == []
    assert "TaskDerived" not in {e.event_type for e in JsonlEventStore(tmp_path / "events").iter_events()}


def test_batch_rejects_invalid_source_heading_leaves_zero_partial(tmp_path: Path):
    store = _store(tmp_path)
    workspace = _job_ws(tmp_path, "job")
    out = workspace / "output"
    missing_heading = (
        "# Deliver behavior\n\n"
        "Implement something.\n"  # missing ## Why / What / How / Acceptance
    )
    _child(out, "good.md", local_id="good")
    _child(out, "bad.md", local_id="bad", body=missing_heading)
    _job(store, "01J00000000000000000000JOB", TASK_ID, workspace)
    adapter = FakeAdapter(_parent())
    # An ImplementationTask child requires the five-part body schema.
    service = CompletionService(workflow=_derived_workflow(task_type="ImplementationTask"),
                                adapter=adapter, job_store=store,
                                event_store=JsonlEventStore(tmp_path / "events"))

    result = _submit(service, "01J00000000000000000000JOB", (
        ArtifactSubmission(type="child_task", path="good.md"),
        ArtifactSubmission(type="child_task", path="bad.md"),
    ))

    assert result.accepted is False
    assert "task.section_missing" in {error.code for error in result.errors}
    assert adapter.created == []
    assert adapter.moved_parents == []
    assert "TaskDerived" not in {e.event_type for e in JsonlEventStore(tmp_path / "events").iter_events()}


def test_batch_repair_publishes_exactly_once(tmp_path: Path):
    store = _store(tmp_path)
    workspace = _job_ws(tmp_path, "job")
    out = workspace / "output"
    _child(out, "good.md", local_id="good")
    _child(out, "bad.md", local_id="bad", deps=("missing",))
    _job(store, "01J00000000000000000000JOB", TASK_ID, workspace)
    adapter = FakeAdapter(_parent())
    events = JsonlEventStore(tmp_path / "events")
    service = CompletionService(workflow=_derived_workflow(), adapter=adapter, job_store=store, event_store=events)

    rejected = _submit(service, "01J00000000000000000000JOB", (
        ArtifactSubmission(type="child_task", path="good.md"),
        ArtifactSubmission(type="child_task", path="bad.md"),
    ))
    assert rejected.accepted is False
    assert adapter.created == []

    # Repair: drop the invalid child; publish the valid one with a new submission.
    repaired = _submit(service, "01J00000000000000000000JOB", (
        ArtifactSubmission(type="child_task", path="good.md"),
    ))
    assert repaired.accepted is True, [e.code for e in repaired.errors]
    assert len(adapter.created) == 1
    assert adapter.created[0].parent_id == TASK_ID

    # Re-submitting the accepted repair must not duplicate siblings.
    replay = _submit(service, "01J00000000000000000000JOB", (
        ArtifactSubmission(type="child_task", path="good.md"),
    ))
    assert replay.accepted is True
    assert len(adapter.created) == 1


def test_batch_blocks_on_existing_unfinished_prerequisite_with_precise_diagnostic(tmp_path: Path):
    store = _store(tmp_path)
    workspace = _job_ws(tmp_path, "job")
    out = workspace / "output"
    # A dependency references an existing unfinished persisted task "5".
    _child(out, "new.md", local_id="new", deps=("5",))
    existing = _parent()
    adapter = FakeAdapter(existing)
    # Simulate a persisted unfinished task id "5" that is already in the tracker.
    adapter.create_task(Task(id="5", title="Existing prerequisite", path="tasks/5.md",
                             current_state="Todo", task_type="chunk"))
    adapter.created.clear()
    _job(store, "01J00000000000000000000JOB", TASK_ID, workspace)
    events = JsonlEventStore(tmp_path / "events")
    service = CompletionService(workflow=_derived_workflow(), adapter=adapter, job_store=store, event_store=events,
                                artifact_root=tmp_path / "artifacts")

    result = _submit(service, "01J00000000000000000000JOB", (
        ArtifactSubmission(type="child_task", path="new.md"),
    ))

    assert result.accepted is False
    codes = {error.code for error in result.errors}
    assert "task.derived_existing_prerequisite_unrepresentable" in codes
    assert any("existing unfinished task 5" in error.message for error in result.errors)
    assert adapter.created == []
    assert "TaskDerived" not in {e.event_type for e in events.iter_events()}


def test_two_valid_batches_allocate_unique_ids_no_cross_parent(tmp_path: Path):
    store = _store(tmp_path)
    events = JsonlEventStore(tmp_path / "events")
    shared = FakeAdapter(_parent("P1"))
    shared.seed(_parent("P2"))

    for name, task_id in (("job-a", "P1"), ("job-b", "P2")):
        workspace = _job_ws(tmp_path, name)
        out = workspace / "output"
        _child(out, "child-one.md", local_id="child-one")
        _child(out, "child-two.md", local_id="child-two")
        _job(store, name, task_id, workspace)

    service_a = CompletionService(workflow=_derived_workflow(), adapter=shared, job_store=store, event_store=events)
    service_b = CompletionService(workflow=_derived_workflow(), adapter=shared, job_store=store, event_store=events)

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def run(job_id: str, artifact_path: str, service) -> None:
        barrier.wait()
        results[job_id] = service.submit(
            job_id=job_id, token="secret",
            submission=CompletionSubmission(
                summary="decomposed",
                artifacts=(ArtifactSubmission(type="child_task", path=artifact_path),),
            ),
        )

    t1 = threading.Thread(target=run, args=("job-a", "child-one.md", service_a))
    t2 = threading.Thread(target=run, args=("job-b", "child-two.md", service_b))
    t1.start(); t2.start(); t1.join(); t2.join()

    ra, rb = results["job-a"], results["job-b"]
    assert ra.accepted is True and rb.accepted is True
    ids = [task.id for task in shared.created if task.task_type == "chunk"]
    assert len(ids) == len(set(ids)), f"ID collision: {ids}"
    # Each child links only to its own parent.
    by_parent: dict[str, list[str]] = {}
    for task in shared.created:
        if task.task_type == "chunk":
            by_parent.setdefault(task.parent_id, []).append(task.id)
    assert set(by_parent) == {"P1", "P2"}
    assert len(by_parent["P1"]) == 1 and len(by_parent["P2"]) == 1
