from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.domain import (
    DomainError,
    EventActor,
    EventType,
    ExecutionJob,
    ArtifactTypeDefinition,
    DerivesDefinition,
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

    def create_task(self, task: Task) -> WriteResult:
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


def test_completion_replays_terminal_accepted_job_as_idempotent_success(tmp_path: Path):
    store = _job_store(tmp_path)
    assert store.update_status("01J00000000000000000000JOB", "accepted").accepted is True
    events = JsonlEventStore(tmp_path / "events")
    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=events,
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(summary="late duplicate"),
    )

    assert result.accepted is True
    assert tuple(events.iter_events()) == ()


def test_completion_marks_terminal_failed_job_submission_as_ignored(tmp_path: Path):
    store = _job_store(tmp_path)
    assert store.update_status("01J00000000000000000000JOB", "failed").accepted is True
    events = JsonlEventStore(tmp_path / "events")
    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=events,
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(summary="late duplicate"),
    )

    assert result.accepted is False
    assert result.errors[0].code == "completion.job_terminal"
    assert [event.event_type for event in events.iter_events()] == ["ExecutionCompletionIgnored"]


def test_completion_promotes_changed_files_into_repo_root(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        repo_root=repo,
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="done",
            artifacts=("result.md",),
            changed_files=("src/main.ts",),
        ),
    )

    assert result.accepted is True
    assert (repo / "src" / "main.ts").read_text(encoding="utf-8") == "export const answer = 42;\n"


def test_completion_commits_promoted_repo_changes_with_task_title(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    (repo / ".git").mkdir(parents=True)
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.ts").write_text("export const answer = 42;\n", encoding="utf-8")

    def runner(command: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        repo_root=repo,
        repo_command_runner=runner,
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="done",
            artifacts=("result.md",),
            changed_files=("src/main.ts",),
        ),
    )

    assert result.accepted is True
    assert calls == [
        (("git", "add", "--", "src/main.ts"), repo),
        (("git", "commit", "-m", "01J00000000000000000000001: Implement thing", "--", "src/main.ts"), repo),
    ]


def test_completion_skips_commit_when_changed_files_match_repo_root(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "main.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.ts").write_text("export const answer = 42;\n", encoding="utf-8")

    def runner(command: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        repo_root=repo,
        repo_command_runner=runner,
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="done",
            artifacts=("result.md",),
            changed_files=("src/main.ts",),
        ),
    )

    assert result.accepted is True
    assert calls == []
    loaded = store.get("01J00000000000000000000JOB")
    assert loaded.job is not None
    assert loaded.job.metadata["promoted_files"] == []


def test_completion_treats_git_nothing_to_commit_as_success(tmp_path: Path):
    store = _job_store(tmp_path)
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    (repo / ".git").mkdir(parents=True)
    (workspace / "output" / "result.md").write_text("done\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.ts").write_text("export const answer = 42;\n", encoding="utf-8")

    def runner(command: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        if command[:2] == ("git", "commit"):
            return subprocess.CompletedProcess(
                command,
                1,
                "On branch master\nnothing to commit, working tree clean\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    service = CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(_task()),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        repo_root=repo,
        repo_command_runner=runner,
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="done",
            artifacts=("result.md",),
            changed_files=("src/main.ts",),
        ),
    )

    assert result.accepted is True
    assert calls == [
        (("git", "add", "--", "src/main.ts"), repo),
        (("git", "commit", "-m", "01J00000000000000000000001: Implement thing", "--", "src/main.ts"), repo),
    ]


def test_completion_derives_child_tasks_and_links_parent(tmp_path: Path):
    store = _job_store(tmp_path)
    output = tmp_path / "workspace" / "output"
    (output / "one.md").write_text(
        "---\nlocal_id: one\n---\n# First child\n\nDo first.\n",
        encoding="utf-8",
    )
    (output / "two.md").write_text(
        "---\nlocal_id: two\ndependencies: [one]\n---\n# Second child\n\nDo second.\n",
        encoding="utf-8",
    )

    class DeriveAdapter(FakeAdapter):
        def __init__(self, task: Task):
            super().__init__(task)
            self.created: list[Task] = []
            self.written_parent: Task | None = None

        def create_task(self, task: Task) -> WriteResult:
            self.created.append(task)
            return WriteResult(path=task.path)

        def write_task(self, task: Task) -> WriteResult:
            self.written_parent = task
            self.task = task
            return WriteResult(path=task.path)

    base = _workflow()
    workflow = WorkflowDefinition(
        schema_version=base.schema_version,
        states=base.states,
        task_types=MappingProxyType({
            **dict(base.task_types),
            "chunk": TaskTypeDefinition(id="chunk", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({
            "child_task": ArtifactTypeDefinition(id="child_task"),
        }),
        validation_types=base.validation_types,
        operation_types=base.operation_types,
        workers=base.workers,
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex",
                requires=RequirementDefinition(),
                transaction=None,
                derives=DerivesDefinition(task_type="chunk", state="Todo", artifact_type="child_task"),
            ),
        }),
    )
    adapter = DeriveAdapter(_task())
    service = CompletionService(
        workflow=workflow,
        adapter=adapter,
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="decomposed",
            artifacts=(
                ArtifactSubmission(type="child_task", path="one.md"),
                ArtifactSubmission(type="child_task", path="two.md"),
            ),
        ),
    )

    assert result.accepted is True
    assert len(adapter.created) == 2
    assert [task.id for task in adapter.created] == ["1", "2"]
    assert adapter.created[0].parent_id == TASK_ID
    assert adapter.created[1].dependencies == ("1",)
    assert adapter.written_parent is not None
    assert "## Derived tasks" in adapter.written_parent.body
    assert [event.event_type for event in JsonlEventStore(tmp_path / "events").iter_events()].count("TaskDerived") == 2


def test_completion_derives_child_tasks_after_existing_numeric_ids(tmp_path: Path):
    store = _job_store(tmp_path)
    output = tmp_path / "workspace" / "output"
    (output / "child.md").write_text(
        "---\nlocal_id: child\n---\n# Child\n\nDo child.\n",
        encoding="utf-8",
    )

    class NumericAdapter(FakeAdapter):
        def __init__(self, task: Task):
            super().__init__(task)
            self.created: list[Task] = []

        def load_project(self) -> LoadProjectResult:
            base = self.task
            numeric = Task(
                id="7",
                title="Existing numeric",
                path="tasks/7-existing.md",
                current_state="Todo",
                task_type="task",
            )
            return LoadProjectResult(snapshot=ProjectSnapshot(
                project_id="Agent",
                tasks=MappingProxyType({base.id: base, numeric.id: numeric}),
                board_positions=MappingProxyType({}),
            ))

        def create_task(self, task: Task) -> WriteResult:
            self.created.append(task)
            return WriteResult(path=task.path)

    base = _workflow()
    workflow = WorkflowDefinition(
        schema_version=base.schema_version,
        states=base.states,
        task_types=MappingProxyType({
            **dict(base.task_types),
            "chunk": TaskTypeDefinition(id="chunk", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({"child_task": ArtifactTypeDefinition(id="child_task")}),
        validation_types=base.validation_types,
        operation_types=base.operation_types,
        workers=base.workers,
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex",
                requires=RequirementDefinition(),
                transaction=None,
                derives=DerivesDefinition(task_type="chunk", state="Todo", artifact_type="child_task"),
            ),
        }),
    )
    adapter = NumericAdapter(_task())
    service = CompletionService(
        workflow=workflow,
        adapter=adapter,
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
    )

    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(
            summary="decomposed",
            artifacts=(ArtifactSubmission(type="child_task", path="child.md"),),
        ),
    )

    assert result.accepted is True
    assert [task.id for task in adapter.created] == ["8"]


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
