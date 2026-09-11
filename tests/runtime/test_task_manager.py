from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.domain import (
    EventActor,
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkerDefinition,
    WorkflowDefinition,
)
from open_tulid.runtime import CreateExecutionJob, RequestTransition, TaskManager, ValidateProject


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
        return WriteResult(path=task_id)

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
                requires=RequirementDefinition(),
                transaction=None,
            ),
        }),
    )


def _snapshot(task: Task | None = None) -> ProjectSnapshot:
    task = task or Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
    )
    return ProjectSnapshot(project_id="Agent", tasks=MappingProxyType({task.id: task}), board_positions=MappingProxyType({}))


def test_validate_project_accepts_snapshot_matching_workflow():
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(_snapshot()))

    result = manager.handle(ValidateProject(project_id="Agent"))

    assert result.accepted is True
    assert result.errors == ()


def test_validate_project_rejects_unknown_state():
    bad_task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Unknown",
        task_type="task",
    )
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(_snapshot(bad_task)))

    result = manager.handle(ValidateProject(project_id="Agent"))

    assert result.accepted is False
    assert result.errors[0].code == "task.unknown_state"


def test_request_transition_accepts_matching_task_state():
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(_snapshot()))

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
    ))

    assert result.accepted is True
    assert result.events[0].event_type == "TransitionAccepted"
    assert result.events[1].event_type == "TaskMoved"
    assert result.effects == ({
        "type": "move_task",
        "task_id": TASK_ID,
        "from_state": "Todo",
        "to_state": "CodeReview",
    },)


def test_request_transition_rejects_wrong_state():
    task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="CodeReview",
        task_type="task",
    )
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(_snapshot(task)))

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
    ))

    assert result.accepted is False
    assert result.errors[0].code == "transition.state_mismatch"


def test_request_transition_rejects_missing_target_state_artifact():
    workflow = _workflow_with_review_artifact_requirement()
    manager = TaskManager(workflow=workflow, adapter=FakeAdapter(_snapshot()))

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
    ))

    assert result.accepted is False
    assert result.errors[0].code == "task.required_artifact_missing"


def test_validate_project_checks_current_state_artifact_requirements():
    task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="CodeReview",
        task_type="task",
    )
    manager = TaskManager(
        workflow=_workflow_with_review_artifact_requirement(),
        adapter=FakeAdapter(_snapshot(task)),
    )

    result = manager.handle(ValidateProject(project_id="Agent"))

    assert result.accepted is False
    assert result.errors[0].code == "task.required_artifact_missing"


def _success_workflow() -> WorkflowDefinition:
    """Implementation-completing transition into a declared success terminal."""
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "Done": StateDefinition(id="Done", terminal_outcome="success"),
            "Failed": StateDefinition(id="Failed", terminal_outcome="failure"),
            "Blocked": StateDefinition(id="Blocked"),
        }),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({
            "codex": WorkerDefinition(id="codex"),
        }),
        transitions=MappingProxyType({
            "implement": TransitionDefinition(
                id="implement", task_type="task", from_state="Todo", to_state="Done",
                worker="codex", requires=RequirementDefinition(), transaction=None,
            ),
            "clarify": TransitionDefinition(
                id="clarify", task_type="task", from_state="Todo", to_state="Blocked",
                worker=None, requires=RequirementDefinition(), transaction=None,
            ),
            "cancel": TransitionDefinition(
                id="cancel", task_type="task", from_state="Todo", to_state="Failed",
                worker=None, requires=RequirementDefinition(), transaction=None,
            ),
        }),
    )


@dataclass(frozen=True)
class FakeJob:
    status: str
    job_id: str = "job"
    metadata: Mapping[str, Any] = MappingProxyType({})
    project_id: str = "Agent"
    task_id: str = TASK_ID
    transition_id: str = "implement"


@dataclass(frozen=True)
class _FakeJobList:
    jobs: tuple
    accepted: bool = True
    error: Any = None


class FakeJobStore:
    def __init__(self, jobs=()):
        self.jobs = tuple(jobs)

    def list(self):
        return _FakeJobList(jobs=self.jobs)


def test_request_transition_rejects_manual_implementation_without_acceptance():
    # 6C: a manual request moving a task to a success terminal must carry the
    # same committed acceptance evidence as the runtime path. Absent that
    # evidence it is rejected and the state is left intact.
    manager = TaskManager(
        workflow=_success_workflow(),
        adapter=FakeAdapter(_snapshot()),
        history_job_store=FakeJobStore(),
    )

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        actor=EventActor(type="user", id="cli"),
    ))

    assert result.accepted is False
    assert result.errors[0].code == "manual.implementation_require_acceptance"
    assert "verified" in result.errors[0].message.lower()
    assert result.events == ()
    assert result.effects == ()


def test_request_transition_accepts_manual_implementation_with_committed_acceptance(tmp_path):
    # 6C: with a committed acceptance record (accepted job carrying the delivery
    # transaction) present, the manual implementation-completing transition is
    # aligned to the runtime path and moves the card.
    accepted = FakeJob(
        status="accepted",
        task_id=TASK_ID,
        transition_id="implement",
        metadata={"acceptance_transaction_id": f"job-sub"},
        project_id="Agent",
    )
    from open_tulid.runtime.events import TransactionJournalStore
    from open_tulid.runtime.attempts import task_semantic_revision
    journals = TransactionJournalStore(tmp_path / "events/journals")
    record = journals.prepare(journal_id="job-sub", project_id="Agent", task_id=TASK_ID,
        transition_id="implement", effects=(), events=(), context={
            "job_id": accepted.job_id, "task_revision": task_semantic_revision(_snapshot().tasks[TASK_ID]),
            "verification_accepted": True, "expected_to_state": "Done",
        }).record
    journals.commit(record)
    manager = TaskManager(
        project_root=tmp_path,
        workflow=_success_workflow(),
        adapter=FakeAdapter(_snapshot()),
        history_job_store=FakeJobStore((accepted,)),
    )

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        actor=EventActor(type="user", id="cli"),
    ))

    assert result.accepted is True
    assert result.effects == ({
        "type": "move_task",
        "task_id": TASK_ID,
        "from_state": "Todo",
        "to_state": "Done",
    },)


def test_request_transition_rejects_manual_implementation_when_store_unavailable():
    # 6C: if no job store is available to confirm committed acceptance evidence,
    # a manual implementation-completing request cannot claim verified success.
    manager = TaskManager(workflow=_success_workflow(), adapter=FakeAdapter(_snapshot()))

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        actor=EventActor(type="user", id="cli"),
    ))

    assert result.accepted is False
    assert result.errors[0].code in (
        "manual.implementation_acceptance_unverifiable",
        "manual.implementation_require_acceptance",
    )


def test_request_transition_keeps_manual_business_transition_without_acceptance():
    # 6C: manual transitions that intentionally carry no implementation-success
    # guarantee (clarification/block) remain available without acceptance evidence.
    manager = TaskManager(
        workflow=_success_workflow(),
        adapter=FakeAdapter(_snapshot()),
        history_job_store=FakeJobStore(),
    )

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="clarify",
        actor=EventActor(type="user", id="cli"),
    ))

    assert result.accepted is True
    assert result.effects == ({
        "type": "move_task",
        "task_id": TASK_ID,
        "from_state": "Todo",
        "to_state": "Blocked",
    },)


def test_request_transition_keeps_manual_cancel_without_acceptance():
    # 6C: a manual block/cancel terminal (failure) is not a verified success and
    # remains a legitimately manual operation with no acceptance evidence.
    manager = TaskManager(
        workflow=_success_workflow(),
        adapter=FakeAdapter(_snapshot()),
        history_job_store=FakeJobStore(),
    )

    result = manager.handle(RequestTransition(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="cancel",
        actor=EventActor(type="user", id="cli"),
    ))

    assert result.accepted is True
    assert result.effects == ({
        "type": "move_task",
        "task_id": TASK_ID,
        "from_state": "Todo",
        "to_state": "Failed",
    },)


def test_validate_project_accepts_promoted_artifact_link_for_state_requirement():
    task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="CodeReview",
        task_type="task",
        artifact_links=(f"artifacts/{TASK_ID}/patch/result.diff",),
    )
    manager = TaskManager(
        workflow=_workflow_with_review_artifact_requirement(),
        adapter=FakeAdapter(_snapshot(task)),
    )

    assert manager.handle(ValidateProject(project_id="Agent")).accepted is True


def test_create_execution_job_uses_transition_worker_and_workspace(tmp_path: Path):
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(_snapshot()))

    result = manager.handle(CreateExecutionJob(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        workspace_root=tmp_path,
    ))

    assert result.accepted is True
    assert result.job is not None
    assert result.job.worker_id == "codex"
    assert result.job.workspace_path.startswith(str(tmp_path))
    assert [event.event_type for event in result.events] == ["ExecutionJobCreated"]
    assert result.effects[0]["type"] == "create_execution_job"


def test_direct_job_creation_checks_dependency_acceptance(tmp_path):
    from open_tulid.runtime.jobs import FileExecutionJobStore
    task = replace(_snapshot().tasks[TASK_ID], dependencies=("dependency",))
    store = FileExecutionJobStore(tmp_path / "jobs")
    repo = tmp_path / "repo"
    repo.mkdir()
    for state, expected in (("Todo", "task.dependency_unmet"), ("CodeReview", "task.dependency_not_accepted")):
        dependency = replace(task, id="dependency", dependencies=(), current_state=state)
        snapshot = replace(_snapshot(task), tasks={task.id: task, dependency.id: dependency})
        manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(snapshot), job_store=store,
                              history_job_store=store, project_root=tmp_path, repo_root=repo)
        result = manager.create_execution_job(CreateExecutionJob(
            project_id="Agent", task_id=TASK_ID, transition_id="code", workspace_root=tmp_path / "workspaces",
        ))
        assert not result.accepted
        assert result.errors[0].code == expected
        assert not store.list().jobs


def test_planning_job_preserves_task_instructions_and_sources_after_vault_edits(tmp_path):
    from open_tulid.runtime.executor import _frozen_prompt_packet
    from open_tulid.runtime.planning_inputs import load_planning_inputs
    from open_tulid.runtime.workspaces import WorkspacePreparer
    from open_tulid.runtime.jobs import FileExecutionJobStore

    tracker = tmp_path / "tracker"
    (tracker / "agents").mkdir(parents=True)
    instructions = tracker / "agents" / "default.agent.md"
    instructions.write_text("Keep the original planning procedure.")
    reference = tracker / "spec.md"
    reference.write_text("# Decisions\nThe original decision is immutable.")
    task = replace(_snapshot().tasks[TASK_ID], artifact_links=("spec.md",))
    adapter = FakeAdapter(_snapshot(task))
    store = FileExecutionJobStore(tmp_path / "jobs")
    manager = TaskManager(workflow=_workflow(), adapter=adapter, project_root=tracker, job_store=store)
    result = manager.create_execution_job(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code", workspace_root=tmp_path / "workspaces",
    ))
    assert result.accepted, result.errors
    reference.write_text("The live decision has changed.")
    instructions.write_text("The live procedure has changed.")
    changed_task = replace(task, title="A different outcome")
    adapter.snapshot = _snapshot(changed_task)
    job = store.get(result.job.job_id).job
    saved = load_planning_inputs(job)
    assert saved.source_task.title == task.title
    assert "original planning procedure" in _frozen_prompt_packet(job, None)
    assert "live procedure" not in saved.prompt
    prepared = WorkspacePreparer().prepare(job=job, task=changed_task, transition=_workflow().transitions["code"])
    assert prepared.accepted, prepared.error
    assert len(saved.context_files) == 1
    file = saved.context_files[0]
    assert file.workspace_path in saved.prompt
    assert (prepared.workspace / ".open-tulid" / file.workspace_path).read_text() == "# Decisions\nThe original decision is immutable."
    task_context = __import__("json").loads((prepared.workspace / ".open-tulid" / "job-context.json").read_text())
    assert task_context["task"]["title"] == task.title


def test_planning_job_rejects_missing_source_before_persistence(tmp_path):
    from open_tulid.runtime.jobs import FileExecutionJobStore
    task = replace(_snapshot().tasks[TASK_ID], artifact_links=("missing-spec.md",))
    store = FileExecutionJobStore(tmp_path / "jobs")
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(_snapshot(task)), project_root=tmp_path, job_store=store)
    result = manager.create_execution_job(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code", workspace_root=tmp_path / "workspaces",
    ))
    assert not result.accepted
    assert any(error.code == "context.link_not_found" for error in result.errors)
    assert not store.list().jobs


def test_planning_workspace_rejects_corrupt_frozen_packet(tmp_path):
    from open_tulid.runtime.workspaces import WorkspacePreparer
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(_snapshot()))
    result = manager.create_execution_job(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code", workspace_root=tmp_path,
    ))
    assert result.accepted
    metadata = dict(result.job.metadata)
    metadata["planning_inputs"] = {**metadata["planning_inputs"], "prompt": "Tampered requirements"}
    job = replace(result.job, metadata=metadata)
    prepared = WorkspacePreparer().prepare(
        job=job, task=_snapshot().tasks[TASK_ID], transition=_workflow().transitions["code"],
    )
    assert not prepared.accepted
    assert "digest mismatch" in prepared.error.message
    assert not Path(job.workspace_path).exists()


def test_create_execution_job_defers_target_state_artifact_requirements_to_completion(tmp_path: Path):
    manager = TaskManager(
        workflow=_workflow_with_review_artifact_requirement(),
        adapter=FakeAdapter(_snapshot()),
    )

    result = manager.handle(CreateExecutionJob(
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        workspace_root=tmp_path,
    ))

    assert result.accepted is True


def _workflow_with_review_artifact_requirement() -> WorkflowDefinition:
    workflow = _workflow()
    return WorkflowDefinition(
        schema_version=workflow.schema_version,
        states=workflow.states,
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(
                id="task",
                requirements_by_state=MappingProxyType({
                    "CodeReview": RequirementDefinition(artifacts=("patch",)),
                }),
            ),
        }),
        artifact_types=workflow.artifact_types,
        validation_types=workflow.validation_types,
        operation_types=workflow.operation_types,
        workers=workflow.workers,
        transitions=workflow.transitions,
    )


def test_parent_lineage_detects_cycle_before_admission():
    from open_tulid.runtime.task_manager import _validate_parent_lineage

    a = Task(id="a", title="A", path="tasks/a.md", current_state="Todo", parent_id="b")
    b = Task(id="b", title="B", path="tasks/b.md", current_state="Todo", parent_id="a")
    adapter = FakeAdapter(ProjectSnapshot(
        project_id="Agent",
        tasks=MappingProxyType({"a": a, "b": b}),
        board_positions=MappingProxyType({}),
    ))

    errors = _validate_parent_lineage(adapter, a)

    assert errors
    assert errors[0].code == "task.parent_cycle"


def test_parent_lineage_accepts_non_cyclic_lineage_end_at_ancestor():
    from open_tulid.runtime.task_manager import _validate_parent_lineage

    root = Task(id="root", title="Root", path="tasks/root.md", current_state="Todo")
    child = Task(id="child", title="Child", path="tasks/child.md", current_state="Todo", parent_id="root")
    adapter = FakeAdapter(ProjectSnapshot(
        project_id="Agent",
        tasks=MappingProxyType({"root": root, "child": child}),
        board_positions=MappingProxyType({}),
    ))

    assert _validate_parent_lineage(adapter, child) == []


def test_missing_ancestor_blocks_job_admission_without_truncating_context(tmp_path):
    from open_tulid.runtime.jobs import FileExecutionJobStore

    child = replace(_snapshot().tasks[TASK_ID], parent_id="parent")
    parent = replace(child, id="parent", parent_id="missing-grandparent")
    snapshot = replace(_snapshot(child), tasks={child.id: child, parent.id: parent})
    store = FileExecutionJobStore(tmp_path / "jobs")
    manager = TaskManager(workflow=_workflow(), adapter=FakeAdapter(snapshot), job_store=store)
    result = manager.create_execution_job(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code", workspace_root=tmp_path,
    ))
    assert not result.accepted
    assert result.errors[0].code == "task.parent_unavailable"
    assert result.errors[0].location == "missing-grandparent"
    assert not store.list().jobs
