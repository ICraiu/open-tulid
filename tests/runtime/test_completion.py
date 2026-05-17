from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.domain import (
    DomainError,
    EventActor,
    EventType,
    ExecutionJob,
    ArtifactTypeDefinition,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkflowDefinition,
    ValidationCallDefinition,
)
from open_tulid.runtime import (
    ArtifactSubmission,
    CompletionService,
    CompletionSubmission,
    FileExecutionJobStore,
    JsonlEventStore,
    TransactionJournalStore,
    build_event,
    recover_completion_transactions,
)
from open_tulid.workflow.implementations import (
    VALIDATION_IMPLEMENTATIONS,
    WorkflowExecutionContext,
)


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
        if task_id != self.task.id:
            return ReadTaskResult()
        if self.moved_to is None:
            return ReadTaskResult(task=self.task)
        return ReadTaskResult(task=Task(
            id=self.task.id,
            title=self.task.title,
            path=self.task.path,
            current_state=self.moved_to,
            task_type=self.task.task_type,
            dependencies=self.task.dependencies,
            artifact_links=self.task.artifact_links,
            parent_id=self.task.parent_id,
            metadata=self.task.metadata,
            body=self.task.body,
        ))

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


def test_completion_rejects_artifact_path_that_violates_template(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "output" / "wrong.md").write_text("done\n", encoding="utf-8")
    workflow = _workflow()
    workflow = WorkflowDefinition(
        schema_version=workflow.schema_version,
        states=workflow.states,
        task_types=workflow.task_types,
        artifact_types=MappingProxyType({
            "result.md": ArtifactTypeDefinition(id="result.md", template="result.md"),
        }),
        validation_types=workflow.validation_types,
        operation_types=workflow.operation_types,
        workers=workflow.workers,
        transitions=workflow.transitions,
        storage=workflow.storage,
    )
    service = CompletionService(
        workflow=workflow,
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=lambda workspace, output_root: WorkflowExecutionContext(
            project_root=workspace,
            vault_root=output_root,
        ),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="done",
            artifacts=(ArtifactSubmission(type="result.md", path="wrong.md"),),
        ),
    )

    assert result.accepted is False
    assert {error.code for error in result.errors} == {"completion.artifact_template_mismatch"}


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


def test_completion_requires_changed_files_when_transition_demands_them(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    workflow = _workflow_with_requires(RequirementDefinition(
        artifacts=("result.md",),
        changed_files_required=True,
    ))
    service = CompletionService(
        workflow=workflow,
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=lambda workspace, output_root: WorkflowExecutionContext(
            project_root=workspace,
            vault_root=output_root,
        ),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(summary="done", artifacts=("result.md",)),
    )

    assert result.accepted is False
    assert {error.code for error in result.errors} == {"completion.changed_files_missing"}


def test_completion_runs_trusted_validation_instead_of_only_trusting_evidence(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    workflow = _workflow_with_requires(RequirementDefinition(
        artifacts=("result.md",),
        validations=(ValidationCallDefinition(type="tests_pass", args=MappingProxyType({
            "command": ("python", "-c", "import sys; sys.exit(7)"),
        })),),
    ))
    service = CompletionService(
        workflow=workflow,
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=lambda workspace, output_root: WorkflowExecutionContext(
            project_root=workspace,
            vault_root=output_root,
        ),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="done",
            artifacts=("result.md",),
            validation_evidence={"tests_pass": "passed"},
        ),
    )

    assert result.accepted is False
    assert {error.code for error in result.errors} == {"completion.validation_failed"}


def test_completion_rejects_duplicate_submission_entries(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="done",
            artifacts=(
                ArtifactSubmission(type="result.md", path="result.md"),
                ArtifactSubmission(type="result.md", path="result.md"),
            ),
            changed_files=("x.py", "x.py"),
        ),
    )

    assert result.accepted is False
    assert {
        "completion.artifact_duplicate_type",
        "completion.artifact_duplicate_path",
        "completion.changed_file_duplicate",
        "completion.changed_file_not_found",
    }.issubset({error.code for error in result.errors})


def test_completion_compensates_promoted_artifact_when_move_fails(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")

    class MoveFailingAdapter(FakeAdapter):
        def move_task(self, task_id: str, state: str) -> WriteResult:
            return WriteResult(errors=(DomainError("move.failed", "cannot move"),))

    artifact_root = tmp_path / "artifacts"
    service = CompletionService(
        workflow=_workflow(),
        adapter=MoveFailingAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        journal_store=TransactionJournalStore(tmp_path / "events" / "journals"),
        artifact_root=artifact_root,
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(summary="done", artifacts=("result.md",)),
    )

    assert result.accepted is False
    assert not [path for path in artifact_root.rglob("result.md") if path.is_file()]


def test_recover_completion_transactions_finishes_prepared_acceptance(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    (output / "result.md").write_text("done\n", encoding="utf-8")
    adapter = FakeAdapter(_task())
    events = JsonlEventStore(tmp_path / "events")
    journals = TransactionJournalStore(tmp_path / "events" / "journals")
    artifact_root = tmp_path / "artifacts"
    target = artifact_root / TASK_ID / "result.md" / "result.md"
    journal = journals.prepare(
        journal_id="01J00000000000000000000JRN",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        effects=(
            {
                "type": "promote_artifact",
                "task_id": TASK_ID,
                "source_path": str(output / "result.md"),
                "target_path": str(target),
                "link": "artifacts/link.md",
                "target_existed": False,
                "previous_links": (),
            },
            {"type": "move_task", "task_id": TASK_ID, "to_state": "CodeReview"},
        ),
        events=(build_event(
            project_id="Agent",
            actor=EventActor(type="system", id="test"),
            event_type=EventType.ExecutionFinished,
            correlation_id="corr",
            task_id=TASK_ID,
            transition_id="code",
        ),),
    )
    assert journal.accepted is True
    service = CompletionService(
        workflow=_workflow(),
        adapter=adapter,
        job_store=store,
        event_store=events,
        journal_store=journals,
        artifact_root=artifact_root,
    )

    recovered = recover_completion_transactions(
        service=service,
        event_store=events,
        journal_store=journals,
    )

    assert recovered == ("01J00000000000000000000JRN",)
    assert target.is_file()
    assert adapter.moved_to == "CodeReview"
    assert journals.load("01J00000000000000000000JRN").status.value == "committed"


def _workflow_with_requires(requires: RequirementDefinition) -> WorkflowDefinition:
    workflow = _workflow()
    return WorkflowDefinition(
        schema_version=workflow.schema_version,
        states=workflow.states,
        task_types=workflow.task_types,
        artifact_types=workflow.artifact_types,
        validation_types=workflow.validation_types,
        operation_types=workflow.operation_types,
        workers=workflow.workers,
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex",
                requires=requires,
                transaction=None,
            ),
        }),
        storage=workflow.storage,
    )
