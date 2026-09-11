"""R1 — require declared outcomes and accepted evidence on every admission route.

These tests exercise the boundary of scheduler admission and explicit job
creation, plus the manual transition route, so a declared-success dependency
always requires committed acceptance evidence (including a non-Git source root)
and an undeclared terminal is never treated as implicit success.
"""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
import workflow_engine

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
from open_tulid.runtime import (
    CreateExecutionJob,
    FileExecutionJobStore,
    RequestTransition,
    Scheduler,
    TaskManager,
)
from open_tulid.runtime.attempts import task_semantic_revision
from open_tulid.runtime.context import resolve_source_content_identities
from open_tulid.runtime.events import TransactionJournalStore
from open_tulid.runtime.repository_facts import repository_identity

TASK_ID = "01J00000000000000000000001"
DEP_ID = "01J00000000000000000000002"


class FakeAdapter:
    def __init__(self, snapshot: ProjectSnapshot):
        self.snapshot = snapshot
        self.capabilities: frozenset = frozenset({AdapterCapability.LOAD_PROJECT})

    def load_project(self) -> LoadProjectResult:
        return LoadProjectResult(snapshot=self.snapshot)

    def read_task(self, task_id: str) -> ReadTaskResult:
        task = self.snapshot.tasks.get(task_id)
        return ReadTaskResult(task=task) if task else ReadTaskResult()

    def write_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        return WriteResult(path=state)

    def append_event(self, event: object) -> WriteResult:
        return WriteResult(path="events/test.jsonl")


def _snapshot(dep: Task, dependent: Task) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id="Agent",
        tasks=MappingProxyType({dep.id: dep, dependent.id: dependent}),
        board_positions=MappingProxyType({
            dep.id: BoardPosition(board="Work", column=dep.current_state, card_text=dep.title, line=1),
            dependent.id: BoardPosition(board="Work", column=dependent.current_state, card_text=dependent.title, line=2),
        }),
    )


def _workflow(*, done_state: str = "Done") -> WorkflowDefinition:
    """States and transitions for the R1 admission tests.

    - ``ImplementationTask`` moves through ``SelfReview`` into a configurable
      success terminal and is global-contract/code driven.
    - ``QuestionRound`` reaches ``PlanningDone`` artifact-only (no code diff).
    - plain ``task`` reaches the same success terminal without a code contract.
    """
    states = {
        "Todo": StateDefinition(id="Todo"),
        "SelfReview": StateDefinition(id="SelfReview"),
        done_state: StateDefinition(id=done_state, terminal_outcome="success"),
        "Failed": StateDefinition(id="Failed", terminal_outcome="failure"),
        "Cancelled": StateDefinition(id="Cancelled", terminal_outcome="cancelled"),
        "PlanningTodo": StateDefinition(id="PlanningTodo"),
        "PlanningDone": StateDefinition(id="PlanningDone", terminal_outcome="success"),
    }
    transitions = {
        "implement": TransitionDefinition(
            id="implement", task_type="ImplementationTask",
            from_state="Todo", to_state="SelfReview", worker="w1",
            requires=RequirementDefinition(), transaction=None, default_for_scheduler=True,
        ),
        "self_review": TransitionDefinition(
            id="self_review", task_type="ImplementationTask",
            from_state="SelfReview", to_state=done_state, worker="w1",
            requires=RequirementDefinition(), transaction=None, default_for_scheduler=True,
            review=True,
        ),
        "plan": TransitionDefinition(
            id="plan", task_type="QuestionRound",
            from_state="PlanningTodo", to_state="PlanningDone", worker="w2",
            requires=RequirementDefinition(), transaction=None, default_for_scheduler=True,
        ),
        "code1": TransitionDefinition(
            id="code1", task_type="task",
            from_state="Todo", to_state=done_state, worker="w3",
            requires=RequirementDefinition(), transaction=None, default_for_scheduler=True,
        ),
    }
    task_types = {
        "ImplementationTask": TaskTypeDefinition(
            id="ImplementationTask", requirements_by_state=MappingProxyType({}),
        ),
        "QuestionRound": TaskTypeDefinition(
            id="QuestionRound", requirements_by_state=MappingProxyType({}),
        ),
        "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
    }
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType(states),
        task_types=MappingProxyType(task_types),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType(transitions),
    )


def _with_states(workflow: WorkflowDefinition, states: dict[str, StateDefinition]) -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1, states=MappingProxyType(states),
        task_types=workflow.task_types, artifact_types=workflow.artifact_types,
        validation_types=workflow.validation_types, operation_types=workflow.operation_types,
        workers=workflow.workers, transitions=workflow.transitions,
    )


def _init_git(repo: Path) -> tuple[str, str]:
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        )
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (repo / "app.txt").write_text("baseline")
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD").stdout.strip()
    (repo / "app.txt").write_text("accepted implementation")
    git("commit", "-qam", "implementation")
    accepted = git("rev-parse", "HEAD").stdout.strip()
    return baseline, accepted


def _source_identities(project_root: Path | None, task: Task, transition) -> tuple:
    if project_root is None:
        return ()
    return resolve_source_content_identities(
        project_root=project_root, task=task, transition=transition, parent_tasks=(),
    )


def _record_acceptance(
    *,
    store: FileExecutionJobStore,
    project_root: Path | None,
    task: Task,
    transition,
    transition_id: str,
    job_id: str,
    repo_root: Path | None = None,
    code_task: bool = False,
    baseline: str | None = None,
    accepted_commit: str | None = None,
    stale_revision: bool = False,
    review_blocked: bool = False,
    pending_journal: bool = False,
) -> None:
    assert project_root is not None
    journals = TransactionJournalStore(project_root / "events" / "journals")
    source_idents = _source_identities(project_root, task, transition)
    current = task_semantic_revision(task, source_identities=source_idents)
    revision = (
        task_semantic_revision(
            replace(task, body="## What\nOLD-things"), source_identities=source_idents
        )
        if stale_revision else current
    )
    context = {
        "job_id": job_id,
        "task_revision": revision,
        "expected_to_state": task.current_state,
        "verification_accepted": True,
        "candidate_id": "candidate",
        "candidate_manifest_sha256": "manifest",
        "verification_report": {
            "checks": [{"status": "passed"}],
            "candidate_id": "candidate",
            "candidate_manifest_sha256": "manifest",
        },
    }
    if code_task and baseline is not None:
        context["repository_base_commit"] = baseline
    journal_id = job_id + "-acceptance"
    record = journals.prepare(
        journal_id=journal_id, project_id="Agent", task_id=task.id,
        transition_id=transition_id, effects=(), events=(), context=context,
    ).record
    if not pending_journal:
        assert journals.commit(record).accepted
    metadata = {
        "acceptance_transaction_id": journal_id,
        "verification_report": context["verification_report"],
        "review_result": (
            {"behavior": "x", "evidence": "test", "remaining_blockers": ["blocked"]}
            if review_blocked
            else {"behavior": "x", "evidence": "test", "remaining_blockers": []}
        ),
    }
    if code_task and accepted_commit is not None:
        metadata["acceptance_repository_commit"] = accepted_commit
    assert store.update_status(job_id, "accepted", metadata=metadata).accepted


def _seeded(
    tmp_path: Path,
    *,
    dependency: Task,
    dependent: Task,
    code_task: bool = False,
    with_git: bool = False,
    **evidence_kwargs,
) -> tuple[Path, FileExecutionJobStore, Path | None]:
    """Project root, job store with one accepted dependency job, and repo root."""
    project_root = tmp_path / "tracker"
    project_root.mkdir(parents=True, exist_ok=True)
    store = FileExecutionJobStore(tmp_path / "jobs")
    job_id = "01J00000000000000000000JOB"
    transition_id = "self_review" if code_task else "plan"
    transition = _workflow().transitions[transition_id]
    assert store.create(ExecutionJob(
        job_id=job_id, project_id="Agent", task_id=dependency.id,
        transition_id=transition_id, worker_id="w1",
        workspace_path=str(tmp_path / "work"),
    )).accepted is True

    repo_root = None
    baseline = accepted = None
    if with_git:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        baseline, accepted = _init_git(repo_root)
    _record_acceptance(
        store=store, project_root=project_root, task=dependency,
        transition=transition, transition_id=transition_id, job_id=job_id,
        repo_root=repo_root, code_task=code_task,
        baseline=baseline, accepted_commit=accepted if with_git else None,
        **evidence_kwargs,
    )
    return project_root, store, repo_root


def _dependency_only_snapshot(dep: Task, dependent: Task) -> ProjectSnapshot:
    return _snapshot(dep, dependent)


def _assert_no_dependent_job(store: FileExecutionJobStore) -> None:
    assert not any(job.task_id == TASK_ID for job in store.list().jobs)


# --- Acceptance case 1: renamed success terminal "Archived" admits the dependent ---

def test_scheduler_admits_dependent_against_archived_declared_success(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild the thing.",
    )
    project_root, store, repo_root = _seeded(
        tmp_path, dependency=dep, dependent=dependent, code_task=True, with_git=True,
    )
    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=repo_root,
    )

    result = scheduler.schedule_one("Agent")

    assert result.scheduled is True
    assert result.task_id == TASK_ID
    assert not any(skip.code.startswith("task.dependency") for skip in result.skipped)


def test_explicit_job_creation_admits_dependent_against_archived_declared_success(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild the thing.",
    )
    project_root, store, repo_root = _seeded(
        tmp_path, dependency=dep, dependent=dependent, code_task=True, with_git=True,
    )
    # Mirrors the operator CLI `jobs create` manager: job_store None, history_job_store set.
    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=None, history_job_store=store,
        project_root=project_root, repo_root=repo_root,
    )

    result = manager.handle(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code1",
        workspace_root=tmp_path / "workspaces",
    ))

    assert result.accepted is True
    assert result.job is not None
    assert result.job.task_id == TASK_ID


# --- Acceptance case 2: undeclared terminal "Done"/"Failed" refuse with ambiguity ---

@pytest.mark.parametrize("dep_state", ["Done", "Failed"])
def test_undeclared_terminal_is_ambiguous_on_both_entry_points(tmp_path: Path, dep_state):
    # A terminal state named Done/Failed but with no declared outcome is
    # ambiguous (never implicit success).
    workflow = _with_states(
        _workflow(),
        {
            "Todo": StateDefinition(id="Todo"),
            "Moved": StateDefinition(id="Moved"),
        },
    )
    dep = Task(id=DEP_ID, title="Dep", path="tasks/dep.md", current_state=dep_state, task_type="task")
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild the thing.",
    )
    project_root = tmp_path / "tracker"
    project_root.mkdir()
    store = FileExecutionJobStore(tmp_path / "jobs")

    # scheduler entry point
    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=None,
    )
    result = scheduler.schedule_one("Agent")
    assert result.scheduled is False
    assert any(skip.code == "task.dependency_outcome_ambiguous" for skip in result.skipped)
    ambiguous = next(
        skip.message for skip in result.skipped
        if skip.code == "task.dependency_outcome_ambiguous"
    )
    assert dep_state in ambiguous
    _assert_no_dependent_job(store)

    # explicit CLI job creation entry point
    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=None, history_job_store=store,
        project_root=project_root, repo_root=None,
    )
    cli = manager.handle(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code1",
        workspace_root=tmp_path / "workspaces",
    ))
    assert cli.accepted is False
    assert cli.errors[0].code == "task.dependency_outcome_ambiguous"


# --- Acceptance case 3: declared failure/cancellation refuse regardless of spelling ---

@pytest.mark.parametrize("dep_state,outcome", [
    ("Failed", "failure"),
    ("Cancelled", "cancelled"),
])
def test_declared_failure_or_cancelled_refused_on_both_entry_points(tmp_path: Path, dep_state, outcome):
    workflow = _with_states(
        _workflow(),
        {
            "Todo": StateDefinition(id="Todo"),
            "Moved": StateDefinition(id="Moved", terminal_outcome="success"),
            dep_state: StateDefinition(id=dep_state, terminal_outcome=outcome),
        },
    )
    dep = Task(id=DEP_ID, title="Dep", path="tasks/dep.md", current_state=dep_state, task_type="task")
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild the thing.",
    )
    project_root = tmp_path / "tracker"
    project_root.mkdir()
    store = FileExecutionJobStore(tmp_path / "jobs")
    # An old accepted job must not override declared failure/cancelled.
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB", project_id="Agent",
        task_id=DEP_ID, transition_id="review", worker_id="w1",
        workspace_path=str(tmp_path / "work"), status="accepted",
        metadata={"acceptance_transaction_id": "x"},
    )).accepted is True

    # scheduler
    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=None,
    )
    result = scheduler.schedule_one("Agent")
    assert result.scheduled is False
    assert any(skip.code == "task.dependency_failed" for skip in result.skipped)

    # explicit CLI job creation
    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=None, history_job_store=store,
        project_root=project_root, repo_root=None,
    )
    cli = manager.handle(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code1",
        workspace_root=tmp_path / "workspaces",
    ))
    assert cli.accepted is False
    assert cli.errors[0].code == "task.dependency_failed"


# --- Acceptance case 4: manual move to success without evidence is rejected ---

def test_manual_transition_to_success_without_evidence_is_rejected(tmp_path: Path):
    workflow = _workflow()
    dep = Task(id=DEP_ID, title="Dep", path="tasks/dep.md", current_state="Todo", task_type="task")
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(), body="## What\nBuild.",
    )
    project_root = tmp_path / "tracker"
    project_root.mkdir()
    store = FileExecutionJobStore(tmp_path / "jobs")
    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        history_job_store=store, project_root=project_root, repo_root=None,
    )

    result = manager.handle(RequestTransition(
        project_id="Agent", task_id=TASK_ID, transition_id="code1",
    ))

    assert result.accepted is False
    assert result.errors[0].code == "manual.implementation_require_acceptance"


def test_manual_transition_to_non_success_business_move_is_allowed(tmp_path: Path):
    # A deliberately manual business transition into a non-success terminal
    # carries no implementation-success claim and must remain available.
    workflow = _workflow()
    dep = Task(id=DEP_ID, title="Dep", path="tasks/dep.md", current_state="Todo", task_type="task")
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(),
    )
    project_root = tmp_path / "tracker"
    project_root.mkdir()
    store = FileExecutionJobStore(tmp_path / "jobs")
    # Add a failure-terminal manual transition.
    failure_transition = TransitionDefinition(
        id="cancel_to_failed", task_type="task", from_state="Todo", to_state="Failed",
        worker=None, requires=RequirementDefinition(), transaction=None,
    )
    transitions = dict(workflow.transitions)
    transitions[failure_transition.id] = failure_transition
    workflow = WorkflowDefinition(
        schema_version=1, states=workflow.states, task_types=workflow.task_types,
        artifact_types=workflow.artifact_types, validation_types=workflow.validation_types,
        operation_types=workflow.operation_types, workers=workflow.workers,
        transitions=MappingProxyType(transitions),
    )
    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        history_job_store=store, project_root=project_root, repo_root=None,
    )

    result = manager.handle(RequestTransition(
        project_id="Agent", task_id=TASK_ID, transition_id="cancel_to_failed",
    ))

    assert result.accepted is True


# --- Acceptance case 5: each missing/stale condition fails specifically ---

def test_non_git_code_success_requires_committed_evidence(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild.",
    )

    # Without committed evidence, a non-Git code-success dependency is rejected.
    project_root, store, repo_root = _seeded(
        tmp_path / "noev", dependency=dep, dependent=dependent,
        code_task=True, with_git=False, pending_journal=True,
    )
    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=repo_root,
    )
    result = scheduler.schedule_one("Agent")
    assert result.scheduled is False
    assert any(skip.code == "task.dependency_not_accepted" for skip in result.skipped)

    # With committed artifact acceptance but no git commit, a non-Git code task
    # is admitted (evidence suffices; no application diff is invented).
    project_root2, store2, repo2 = _seeded(
        tmp_path / "withev", dependency=dep, dependent=dependent,
        code_task=True, with_git=False,
    )
    scheduler2 = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store2, workspace_root=tmp_path / "workspaces",
        project_root=project_root2, repo_root=repo2,
    )
    result2 = scheduler2.schedule_one("Agent")
    assert result2.scheduled is True


def test_missing_job_store_blocks_with_evidence_unavailable(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild.",
    )
    project_root = tmp_path / "tracker"
    project_root.mkdir()
    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=None, history_job_store=None,
        project_root=project_root, repo_root=None,
    )

    result = manager.handle(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code1",
        workspace_root=tmp_path / "workspaces",
    ))

    assert result.accepted is False
    assert result.errors[0].code == "task.dependency_evidence_unavailable"
    assert "job store" in result.errors[0].message


def test_missing_journal_blocks_with_evidence_unavailable(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild.",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=None, history_job_store=store,
        project_root=None, repo_root=None,
    )

    result = manager.handle(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code1",
        workspace_root=tmp_path / "workspaces",
    ))

    assert result.accepted is False
    assert result.errors[0].code == "task.dependency_evidence_unavailable"
    assert "journal" in result.errors[0].message


def test_stale_semantic_revision_rejects_dependent(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
        body="## What\nRequired behavior now.",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild.",
    )
    project_root, store, repo_root = _seeded(
        tmp_path, dependency=dep, dependent=dependent,
        code_task=True, with_git=True, stale_revision=True,
    )
    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=repo_root,
    )

    result = scheduler.schedule_one("Agent")

    assert result.scheduled is False
    assert any(skip.code == "task.dependency_not_accepted" for skip in result.skipped)


def test_failed_review_rejects_dependent(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
        body="## What\nRequired behavior.",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild.",
    )
    project_root, store, repo_root = _seeded(
        tmp_path, dependency=dep, dependent=dependent,
        code_task=True, with_git=True, review_blocked=True,
    )
    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=repo_root,
    )

    result = scheduler.schedule_one("Agent")

    assert result.scheduled is False
    assert any(skip.code == "task.dependency_not_accepted" for skip in result.skipped)


def test_unrelated_head_rejects_dependent(tmp_path: Path):
    workflow = _workflow(done_state="Archived")
    dep = Task(
        id=DEP_ID, title="Dep", path="tasks/dep.md",
        current_state="Archived", task_type="ImplementationTask",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild.",
    )
    project_root, store, repo_root = _seeded(
        tmp_path, dependency=dep, dependent=dependent, code_task=True, with_git=True,
    )
    # Reset HEAD so the accepted commit is no longer in current history.
    subprocess.run(["git", "-C", str(repo_root), "reset", "--hard", "HEAD~1"],
                   capture_output=True, text=True)
    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=repo_root,
    )

    result = scheduler.schedule_one("Agent")

    assert result.scheduled is False
    assert any(skip.code == "task.dependency_not_accepted" for skip in result.skipped)


# --- Acceptance case 6: artifact-only planning predecessor releases dependent ---

def test_artifact_only_planning_predecessor_releases_dependent(tmp_path: Path):
    workflow = _workflow()
    dep = Task(
        id=DEP_ID, title="Plan", path="tasks/plan.md",
        current_state="PlanningDone", task_type="QuestionRound",
    )
    dependent = Task(
        id=TASK_ID, title="Dependent", path="tasks/dependent.md",
        current_state="Todo", task_type="task", dependencies=(DEP_ID,),
        body="## What\nBuild.",
    )
    project_root, store, _ = _seeded(
        tmp_path, dependency=dep, dependent=dependent, code_task=False, with_git=False,
    )

    scheduler = Scheduler(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store, workspace_root=tmp_path / "workspaces",
        project_root=project_root, repo_root=None,
    )
    result = scheduler.schedule_one("Agent")
    assert result.scheduled is True
    assert result.task_id == TASK_ID

    manager = TaskManager(
        workflow=workflow, adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=None, history_job_store=store,
        project_root=project_root, repo_root=None,
    )
    cli = manager.handle(CreateExecutionJob(
        project_id="Agent", task_id=TASK_ID, transition_id="code1",
        workspace_root=tmp_path / "workspaces",
    ))
    assert cli.accepted is True
    assert cli.job is not None


# --- Proof of test strength: restore implicit success or drop the gate fails ---

def test_legacy_implicit_success_is_never_inferred_from_state_spelling():
    from open_tulid.domain.completion import dependency_outcome, has_outgoing_transition
    workflow = _workflow(done_state="Archived")
    # The migrated current workflow declares success explicitly.
    assert dependency_outcome(workflow, "task", "Archived") == "success"
    # A legacy workflow where terminal Done/Failed carry no declaration must be
    # classified ambiguous, never implicit success, regardless of spelling.
    legacy = WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "Done": StateDefinition(id="Done"),
            "Failed": StateDefinition(id="Failed"),
        }),
        task_types=workflow.task_types,
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=workflow.transitions,
    )
    assert not has_outgoing_transition(legacy, "task", "Done")
    assert dependency_outcome(legacy, "task", "Done") == "ambiguous"
    assert dependency_outcome(legacy, "task", "Failed") == "ambiguous"


def test_dependency_error_blocks_when_evidence_stores_unavailable(tmp_path: Path):
    from open_tulid.runtime.scheduler import _dependency_error

    workflow = _workflow(done_state="Archived")
    snapshot = _snapshot(
        Task(id=DEP_ID, title="Dep", path="tasks/dep.md",
             current_state="Archived", task_type="ImplementationTask"),
        Task(id=TASK_ID, title="Dependent", path="tasks/dependent.md",
             current_state="Todo", task_type="task", dependencies=(DEP_ID,)),
    )
    error = _dependency_error(
        snapshot.tasks[TASK_ID], snapshot, workflow,
        job_store=None, project_id="Agent", repo_root=None, journal_store=None,
    )
    assert error is not None
    assert error.code == "task.dependency_evidence_unavailable"
    # Distinct from the on-the-books acceptance failure; the check is never dropped.
    assert error.code != "task.dependency_failed"
    assert error.code != "task.dependency_unmet"


# --- Acceptance case 7: historical workflow loads with a migration diagnostic ---

def _build_document(yaml_source: str):
    parsed = workflow_engine.parse_yaml(yaml_source)
    assert parsed.value is not None, f"parse failed: {parsed.diagnostics}"
    ast_result = workflow_engine.build_ast(parsed.value)
    assert ast_result.document is not None, f"ast build failed: {ast_result.diagnostics}"
    return ast_result.document


def test_legacy_workflow_loads_with_migration_diagnostic(tmp_path: Path):
    from open_tulid.workflow.compiler import compile_workflow
    doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
  - kind: task_type
    id: task
  - kind: transition
    id: Implement
    task_type: task
    from: Todo
    to: Done
""")

    result = compile_workflow(doc)

    assert result.definition is not None
    warning = next(
        d for d in result.diagnostics
        if d.code == "workflow.compile.ambiguous_terminal_state"
    )
    assert warning.severity == "warning"
