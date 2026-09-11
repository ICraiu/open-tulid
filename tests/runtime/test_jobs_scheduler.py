from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from multiprocessing import get_context
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.containers.runtime import AgentRunResult
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
    ValidationCallDefinition,
    WorkflowDefinition,
)
from open_tulid.models import ProjectConfig, ResourceConfig, RuntimeConfig
from open_tulid.runtime import (
    AttemptRecord,
    FileExecutionJobStore,
    FileResourceLeaseStore,
    JobExecutor,
    JsonlEventStore,
    PREVIEW_JOB_ID,
    Scheduler,
    TransactionJournalStore,
    attempt_id_for,
    attempt_record_to_dict,
    recover_job_creation_transactions,
    render_execution_prompt,
    task_semantic_revision,
)
from open_tulid.runtime.context import resolve_source_content_identities
from open_tulid.runtime.observability import WorkerObservability
from open_tulid.runtime.execution_contracts import (
    GLOBAL_IMPLEMENTATION_CONTRACT_SCHEMA,
    compile_standard_execution_contract,
    compile_task_execution_contract,
    execution_contract_to_dict,
    load_job_execution_contract,
)
from open_tulid.runtime.repository_facts import repository_identity
from open_tulid.runtime.prompts import compile_execution_prompt, find_review_evidence
from open_tulid.runtime.task_contracts import (
    parse_implementation_contract,
    task_source_intent_sha256,
)


TASK_ID = "01J00000000000000000000001"


@dataclass
class FakeAdapter:
    snapshot: ProjectSnapshot
    name: str = "fake"
    capabilities: frozenset[AdapterCapability] = frozenset({AdapterCapability.LOAD_PROJECT})
    moves: list[tuple[str, str]] = field(default_factory=list)

    def load_project(self) -> LoadProjectResult:
        return LoadProjectResult(snapshot=self.snapshot)

    def read_task(self, task_id: str) -> ReadTaskResult:
        task = self.snapshot.tasks.get(task_id)
        return ReadTaskResult(task=task) if task else ReadTaskResult()

    def write_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        self.moves.append((task_id, state))
        task = self.snapshot.tasks.get(task_id)
        if task is not None:
            tasks = dict(self.snapshot.tasks)
            tasks[task_id] = replace(task, current_state=state)
            self.snapshot = replace(
                self.snapshot,
                tasks=MappingProxyType(tasks),
            )
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
            "Done": StateDefinition(id="Done", terminal_outcome="success"),
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


def _global_contract_workflow(*, review: bool = False) -> WorkflowDefinition:
    transitions = {
        "implement": TransitionDefinition(
            id="implement",
            task_type="ImplementationTask",
            from_state="Todo",
            to_state="SelfReview",
            worker="qwen",
            requires=RequirementDefinition(changed_files_required=True),
            transaction=None,
            default_for_scheduler=True,
        ),
        "invalidate_review": TransitionDefinition(
            id="invalidate_review",
            task_type="ImplementationTask",
            from_state="SelfReview",
            to_state="Todo",
            worker=None,
            requires=RequirementDefinition(),
            transaction=None,
        ),
    }
    if review:
        transitions["review"] = TransitionDefinition(
            id="review",
            task_type="ImplementationTask",
            from_state="SelfReview",
            to_state="Done",
            worker="qwen",
            requires=RequirementDefinition(),
            transaction=None,
            default_for_scheduler=True,
        )
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "SelfReview": StateDefinition(id="SelfReview"),
            "Done": StateDefinition(id="Done", terminal_outcome="success"),
        }),
        task_types=MappingProxyType({
            "ImplementationTask": TaskTypeDefinition(
                id="ImplementationTask",
                requirements_by_state=MappingProxyType({}),
            ),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType(transitions),
    )


def _write_global_contract(project_root: Path) -> Path:
    path = project_root / "contract.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: .open-tulid/home
commands:
  - name: tests
    argv: [python, check_repo.py, tests]
    working_directory: .
    timeout_seconds: 300
retry:
  max_attempts: 3
  visible_feedback: true
""",
        encoding="utf-8",
    )
    return path


def _write_contract(project_root: Path, task: Task, *, source_hash: str) -> Task:
    relative = Path(
        "artifacts"
    ) / task.id / "ImplementationContract" / "implementation-contract.yaml"
    path = project_root / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""\
schema: tulid.implementation/v1
source:
  task_id: "{task.id}"
  source_intent_sha256: "{source_hash}"
profile: code_change
objective: Add a health endpoint.
change_surface:
  add: []
  edit: [app.py]
  forbidden: []
requirements: [The endpoint returns ok.]
checks:
  focused:
    - id: health
      argv: [python, check_repo.py, tests]
  invariants: []
""",
        encoding="utf-8",
    )
    return replace(task, artifact_links=(str(relative),))


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


def test_todo_implementation_task_schedules_directly_with_global_contract(tmp_path: Path):
    project_root = tmp_path / "project"
    _write_global_contract(project_root)
    task = Task(
        id=TASK_ID,
        title="Free-form implementation request",
        path="tasks/request.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Please add healthz in whatever structure is appropriate.",
    )
    scheduler = Scheduler(
        workflow=_global_contract_workflow(),
        adapter=FakeAdapter(_snapshot(task)),
        job_store=FileExecutionJobStore(tmp_path / "jobs"),
        workspace_root=tmp_path / "workspaces",
        project_root=project_root,
    )

    result = scheduler.schedule_one("Agent")

    # A Todo ImplementationTask schedules straight to the implementation worker
    # under the project global contract — no per-task contract, no LLM
    # contract-authoring step.
    assert result.accepted is True
    assert result.scheduled is True
    assert result.transition_id == "implement"
    assert result.job is not None
    assert result.job.worker_id == "qwen"
    assert result.job.metadata["execution_contract_sha256"]
    assert result.job.metadata["execution_contract"]["source"]["task"]["body"] == (
        "Please add healthz in whatever structure is appropriate."
    )
    # The frozen contract is the project-global contract.
    frozen = load_job_execution_contract(result.job, required=True)
    assert frozen.contract is not None
    assert frozen.contract.generated_contract.schema == GLOBAL_IMPLEMENTATION_CONTRACT_SCHEMA
    # The task body directly describes the work to the worker.
    assert "Please add healthz" in result.job.metadata["prompt_packet"]
    preview = compile_execution_prompt(frozen.contract)
    assert preview.text == result.job.metadata["prompt_packet"]
    assert preview.manifest.packet_sha256 == result.job.metadata["prompt_packet_sha256"]


def test_todo_implementation_task_creates_no_contract_author_job(tmp_path: Path):
    project_root = tmp_path / "project"
    _write_global_contract(project_root)
    task = Task(
        id=TASK_ID,
        title="Free-form implementation request",
        path="tasks/request.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Please add healthz in whatever structure is appropriate.",
    )
    adapter = FakeAdapter(_snapshot(task))
    event_store = JsonlEventStore(project_root / "events")
    scheduler = Scheduler(
        workflow=_global_contract_workflow(),
        adapter=adapter,
        job_store=FileExecutionJobStore(tmp_path / "jobs"),
        workspace_root=tmp_path / "workspaces",
        event_store=event_store,
        project_root=project_root,
    )

    result = scheduler.schedule_one("Agent")

    # No codex_contract/PrepareExecutionContract worker job is ever created; the
    # single produced job is the implementation worker itself.
    assert result.scheduled is True
    assert result.job is not None
    assert result.job.transition_id == "implement"
    assert result.job.worker_id == "qwen"
    assert adapter.moves == []


def test_scheduler_does_not_enter_contract_preparation_or_invalidate_todo(tmp_path: Path):
    project_root = tmp_path / "project"
    _write_global_contract(project_root)
    task = Task(
        id=TASK_ID,
        title="Free-form implementation request",
        path="tasks/request.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Please add healthz in whatever structure is appropriate.",
    )
    adapter = FakeAdapter(_snapshot(task))
    event_store = JsonlEventStore(project_root / "events")
    scheduler = Scheduler(
        workflow=_global_contract_workflow(),
        adapter=adapter,
        job_store=FileExecutionJobStore(tmp_path / "jobs"),
        workspace_root=tmp_path / "workspaces",
        event_store=event_store,
        project_root=project_root,
    )

    result = scheduler.schedule_one("Agent")

    assert result.scheduled is True
    assert result.transition_id == "implement"
    assert adapter.moves == []
    assert [event.event_type for event in result.events] == ["ExecutionJobCreated"]
    assert not any(e.event_type == "ContractInvalidated" for e in result.events)


def test_scheduler_compiles_self_review_from_prior_verification_evidence(tmp_path: Path):
    project_root = tmp_path / "project"
    _write_global_contract(project_root)
    task = Task(
        id=TASK_ID,
        title="Free-form implementation request",
        path="tasks/request.md",
        current_state="SelfReview",
        task_type="ImplementationTask",
        body="Please add healthz in whatever structure is appropriate.",
    )
    workflow = _global_contract_workflow(review=True)
    prior_contract = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=None,
        task=task,
        transition=workflow.transitions["implement"],
    )
    assert prior_contract.contract is not None
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J0000000000000000000IMPL",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="qwen",
        workspace_path=str(tmp_path / "implementation-workspace"),
        status="accepted",
        metadata={
            "execution_contract": execution_contract_to_dict(prior_contract.contract),
            "execution_contract_sha256": prior_contract.contract.sha256,
            "verification_report": {
                "schema": "tulid.verification/v1",
                "baseline_sha256": prior_contract.contract.baseline_manifest.sha256,
                "changes": {
                    "added": [],
                    "edited": ["app.py"],
                    "removed": [],
                    "renamed": [],
                    "changed_lines": 2,
                },
                "checks": [{"id": "health", "status": "passed", "exit_code": 0}],
            },
        },
    )).accepted is True
    _record_dependency_acceptance(tmp_path, store, task, project_root=project_root)
    scheduler = Scheduler(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=project_root,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.job is not None
    assert result.job.transition_id == "review"
    assert result.job.metadata["review_source_job_id"] == "01J0000000000000000000IMPL"
    assert result.job.metadata["prompt_manifest"]["packet_type"] == "self_review"
    assert "## Prior Implementation Evidence" in result.job.metadata["prompt_packet"]
    assert '"edited":["app.py"]' in result.job.metadata["prompt_packet"]


def test_preview_matches_scheduled_implementation_packet_from_identical_inputs(tmp_path: Path):
    """Preview must compile the same resolver/compiler as scheduling.

    Preview uses a synthetic job identity and no scheduler mutation, so from
    identical inputs its substantive packet and bundle identity must equal the
    frozen scheduled packet exactly (the compiled route emits no ephemeral
    completion fields).
    """
    project_root = tmp_path / "project"
    _write_global_contract(project_root)
    task = Task(
        id=TASK_ID,
        title="Add healthz endpoint",
        path="tasks/request.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Please add healthz in whatever structure is appropriate.",
    )
    workflow = _global_contract_workflow()
    store = FileExecutionJobStore(tmp_path / "jobs")
    scheduler = Scheduler(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=project_root,
    )
    scheduled = scheduler.schedule_one("Agent")
    assert scheduled.accepted is True
    assert scheduled.scheduled is True
    assert scheduled.job is not None
    frozen = scheduled.job
    assert frozen.metadata["prompt_manifest"]["packet_type"] == "implementation"
    frozen_packet = frozen.metadata["prompt_packet"]
    frozen_contract_sha = frozen.metadata["execution_contract_sha256"]

    compiled_contract = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=None,
        task=task,
        transition=workflow.transitions["implement"],
    )
    assert compiled_contract.contract is not None
    contract = compiled_contract.contract
    preview = render_execution_prompt(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(task)),
        task=contract.source_task,
        transition=contract.transition,
        worker_id=contract.transition.worker,
        job_id=PREVIEW_JOB_ID,
        completion_endpoint=f"http://preview.invalid/jobs/{PREVIEW_JOB_ID}/complete",
        execution_contract=contract,
    )
    assert preview.accepted is True
    assert preview.compiled_prompt is not None
    assert preview.execution_contract_sha256 == frozen_contract_sha
    assert preview.text == frozen_packet
    assert preview.compiled_prompt.manifest.packet_sha256 == frozen.metadata["prompt_manifest"]["packet_sha256"]
    assert preview.text.count("curl -sS -X POST") == 1
    # Preview must not mutate scheduler state: only the one scheduled job exists.
    assert [job.job_id for job in store.list().jobs] == [frozen.job_id]


def test_preview_matches_scheduled_review_packet_from_identical_evidence(tmp_path: Path):
    project_root = tmp_path / "project"
    _write_global_contract(project_root)
    task = Task(
        id=TASK_ID,
        title="Free-form implementation request",
        path="tasks/request.md",
        current_state="SelfReview",
        task_type="ImplementationTask",
        body="Please add healthz in whatever structure is appropriate.",
    )
    workflow = _global_contract_workflow(review=True)
    prior_contract = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=None,
        task=task,
        transition=workflow.transitions["implement"],
    )
    assert prior_contract.contract is not None
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J0000000000000000000IMPL",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="qwen",
        workspace_path=str(tmp_path / "implementation-workspace"),
        status="accepted",
        metadata={
            "execution_contract": execution_contract_to_dict(prior_contract.contract),
            "execution_contract_sha256": prior_contract.contract.sha256,
            "verification_report": {
                "schema": "tulid.verification/v1",
                "baseline_sha256": prior_contract.contract.baseline_manifest.sha256,
                "changes": {
                    "added": [],
                    "edited": ["app.py"],
                    "removed": [],
                    "renamed": [],
                    "changed_lines": 2,
                },
                "checks": [{"id": "health", "status": "passed", "exit_code": 0}],
            },
        },
    )).accepted is True

    _record_dependency_acceptance(tmp_path, store, task, project_root=project_root)
    scheduler = Scheduler(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=project_root,
    )
    scheduled = scheduler.schedule_one("Agent")
    assert scheduled.accepted is True
    assert scheduled.scheduled is True
    assert scheduled.job is not None
    frozen = scheduled.job
    assert frozen.metadata["prompt_manifest"]["packet_type"] == "self_review"
    frozen_packet = frozen.metadata["prompt_packet"]
    frozen_contract_sha = frozen.metadata["execution_contract_sha256"]

    review_contract = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=None,
        task=task,
        transition=workflow.transitions["review"],
    )
    assert review_contract.contract is not None
    evidence = find_review_evidence(
        store.list().jobs,
        project_id="Agent",
        task_id=TASK_ID,
        review_transition=workflow.transitions["review"],
    )
    assert evidence is not None
    contract = review_contract.contract
    preview = render_execution_prompt(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(task)),
        task=contract.source_task,
        transition=contract.transition,
        worker_id=contract.transition.worker,
        job_id=PREVIEW_JOB_ID,
        completion_endpoint=f"http://preview.invalid/jobs/{PREVIEW_JOB_ID}/complete",
        execution_contract=contract,
        review_evidence=evidence,
    )
    assert preview.accepted is True
    assert preview.execution_contract_sha256 == frozen_contract_sha
    assert preview.text == frozen_packet
    assert "## Prior Implementation Evidence" in preview.text
    assert preview.text.count("curl -sS -X POST") == 1
    # Preview never writes a new job.
    assert [job.job_id for job in store.list().jobs] == ["01J0000000000000000000IMPL", frozen.job_id]


def test_scheduler_rejects_scheduling_when_global_contract_is_missing(tmp_path: Path):
    project_root = tmp_path / "project"
    task = Task(
        id=TASK_ID,
        title="Free-form implementation request",
        path="tasks/request.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Please add healthz in whatever structure is appropriate.",
    )
    scheduler = Scheduler(
        workflow=_global_contract_workflow(),
        adapter=FakeAdapter(_snapshot(task)),
        job_store=FileExecutionJobStore(tmp_path / "jobs"),
        workspace_root=tmp_path / "workspaces",
        project_root=project_root,
    )

    result = scheduler.schedule_one("Agent")

    # Without the project global contract, scheduling fails cleanly at creation
    # time and never moves the task back to Todo or fires a contract invalidation.
    assert result.accepted is False
    assert result.scheduled is False
    assert FileExecutionJobStore(tmp_path / "jobs").list().jobs == ()


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


def test_todo_implementation_task_resolves_declared_tests_without_generated_contract(tmp_path: Path):
    # A task may naturally require tests. Tulid must not invent a per-task
    # contract around them: the task body carries the request and the frozen
    # global contract resolves the deterministic test checks to run afterward.
    project_root = tmp_path / "project"
    _write_global_contract(project_root)
    workflow = replace(
        _global_contract_workflow(),
        validation_types=MappingProxyType({}),
        transitions=MappingProxyType({
            **_global_contract_workflow().transitions,
            "implement": TransitionDefinition(
                id="implement",
                task_type="ImplementationTask",
                from_state="Todo",
                to_state="SelfReview",
                worker="qwen",
                requires=RequirementDefinition(
                    changed_files_required=True,
                    validations=(ValidationCallDefinition(
                        type="tests_pass",
                        args=MappingProxyType(
                            {"command": "python check_repo.py tests"},
                        ),
                    ),),
                ),
                transaction=None,
                default_for_scheduler=True,
            ),
        }),
    )
    task = Task(
        id=TASK_ID,
        title="Add tests for healthz",
        path="tasks/request.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Implement healthz plus a unit test in tests/test_healthz.py.",
    )
    scheduler = Scheduler(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(task)),
        job_store=FileExecutionJobStore(tmp_path / "jobs"),
        workspace_root=tmp_path / "workspaces",
        project_root=project_root,
    )
    result = scheduler.schedule_one("Agent")

    assert result.scheduled is True
    assert result.job is not None
    # The task body directly describes the test requirement.
    assert "unit test in tests/test_healthz.py" in result.job.metadata["prompt_packet"]
    frozen = load_job_execution_contract(result.job, required=True)
    assert frozen.contract is not None
    # The frozen global contract resolves the project's configured global command
    # (tests); the transition-declared test stays an ordinary validation and is
    # not turned into a generated contract check.
    resolved_ids = {check.id for check in frozen.contract.resolved_checks}
    assert "tests" in resolved_ids
    assert "tests_pass" not in resolved_ids
    validation_ids = [call.type for call in frozen.contract.transition.requires.validations]
    assert "tests_pass" in validation_ids


def test_legacy_per_task_contract_artifact_remains_readable_and_migratable(tmp_path: Path):
    project_root = tmp_path / "project"
    task = Task(
        id=TASK_ID,
        title="Free-form implementation request",
        path="tasks/request.md",
        current_state="ReadyToImplement",
        task_type="task",
        body="Please add healthz in whatever structure is appropriate.",
    )
    task = _write_contract(
        project_root,
        task,
        source_hash=task_source_intent_sha256(task),
    )
    # The legacy per-task artifact is still located and parsed by the historical
    # loader path (kept for backward compatibility and migration), so old
    # executions and frozen contracts never become unreadable.
    legacy = compile_task_execution_contract(
        project_root=project_root,
        repo_root=None,
        task=task,
        transition=_legacy_prepare_workflow()["implement"],
    )
    assert legacy.accepted is True
    assert legacy.contract is not None
    assert legacy.contract.generated_contract.schema == "tulid.implementation/v1"
    # Round-trip through the frozen job loader.
    loaded = load_job_execution_contract(
        ExecutionJob(
            job_id="01J00000000000000000000JOB",
            project_id="Agent",
            task_id=TASK_ID,
            transition_id="implement",
            worker_id="qwen",
            workspace_path=str(tmp_path / "work"),
            metadata={
                "execution_contract": execution_contract_to_dict(legacy.contract),
                "execution_contract_sha256": legacy.contract.sha256,
            },
        ),
        required=True,
    )
    assert loaded.accepted is True
    assert loaded.contract is not None
    assert loaded.contract.generated_contract.source_task_id == TASK_ID
    # Re-parsing the historical artifact directly still works.
    parsed = parse_implementation_contract(
        (project_root / task.artifact_links[0]).read_text(encoding="utf-8"),
        expected_task_id=TASK_ID,
        expected_source_intent_sha256=task_source_intent_sha256(task),
    )
    assert parsed.accepted is True and parsed.contract is not None


def _legacy_prepare_workflow() -> dict[str, object]:
    return {
        "implement": TransitionDefinition(
            id="implement",
            task_type="task",
            from_state="ReadyToImplement",
            to_state="SelfReview",
            worker="qwen",
            requires=RequirementDefinition(),
            transaction=None,
            default_for_scheduler=True,
        ),
    }


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


def test_scheduler_failed_worker_yields_fresh_attempt_without_backoff(tmp_path: Path):
    """A failed worker is treated uniformly: with backoff disabled, the same
    task is scheduled for a fresh attempt and no classification is consulted."""
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="qwen",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": datetime.now(timezone.utc).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=0,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.job is not None
    assert result.job.job_id != "01J00000000000000000000JOB"
    assert [error.code for error in result.skipped] == []


def test_scheduler_failed_worker_respects_configured_backoff(tmp_path: Path):
    """All worker failures observe the pre-existing configurable backoff."""
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="qwen",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={"updated_at": datetime.now(timezone.utc).isoformat()},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=60,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.recent_failure"


def test_scheduler_fresh_attempt_after_worker_unexpected_exit_is_immediate(tmp_path: Path):
    """A worker_unexpected_exit failure does not count toward the recent-failure
    backoff: the same task is scheduled for an immediate fresh attempt."""
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="qwen",
        workspace_path=str(tmp_path / "work"),
        status="failed",
        metadata={
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "failure_reason": "worker_unexpected_exit",
        },
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=60,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.job is not None
    assert result.job.job_id != "01J00000000000000000000JOB"
    assert [error.code for error in result.skipped] == []


def test_scheduler_failed_workers_still_respect_retry_limit(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    now = datetime.now(timezone.utc).isoformat()
    for index in range(2):
        assert store.create(ExecutionJob(
            job_id=f"01J00000000000000000000J{index:02d}",
            project_id="Agent",
            task_id=TASK_ID,
            transition_id="implement",
            worker_id="qwen",
            workspace_path=str(tmp_path / f"work-{index}"),
            status="failed",
            metadata={
                "updated_at": now,
            },
        )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        max_failed_attempts_per_transition=2,
        runtime_session_started_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        failed_job_backoff_seconds=0,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.retry_limit_reached"


def test_scheduler_unclassified_failed_job_keeps_existing_backoff(tmp_path: Path):
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


def test_e2e_worker_dies_without_completion_driver_schedules_fresh_attempt(tmp_path: Path, monkeypatch):
    """Drive schedule -> run -> unexpected worker exit -> schedule; the same task
    gets an immediate fresh attempt and its Kanban state is left unchanged."""
    store = FileExecutionJobStore(tmp_path / "jobs")
    events = JsonlEventStore(tmp_path / "events")
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    adapter = FakeAdapter(_snapshot())
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=adapter,
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=60,
    )

    first = scheduler.schedule_one("Agent")
    assert first.accepted is True and first.job is not None
    first_job_id = first.job.job_id

    class ControllableProbe:
        def __init__(self) -> None:
            self._dead = threading.Event()
        def die(self) -> None:
            self._dead.set()
        def is_alive(self) -> bool:
            return not self._dead.is_set()

    worker_started = threading.Event()
    probe = ControllableProbe()

    def fake_run_agent_container(request, *, docker_executable):
        worker_started.set()
        threading.Event().wait()

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)

    executor = JobExecutor(
        workflow=_workflow(),
        adapter=adapter,
        job_store=store,
        event_store=events,
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_args={"codex": ("exec", "{prompt_packet}")},
            default_timeout_seconds=1,
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent", repo_root=tmp_path / "repo"),
        observability=WorkerObservability(check_interval_seconds=0.001),
        liveness_probe=probe,
    )
    run_box: dict[str, object] = {}
    run_thread = threading.Thread(
        target=lambda: run_box.update(result=executor.run(first_job_id)),
        name="test-driver-run",
    )
    run_thread.start()
    assert worker_started.wait(timeout=5)
    probe.die()
    run_thread.join(timeout=15)
    result = run_box["result"]

    assert result.accepted is True
    assert result.run is None
    failed_job = store.get(first_job_id).job
    assert failed_job is not None
    assert failed_job.status == "failed"
    assert failed_job.metadata["failure_reason"] == "worker_unexpected_exit"

    second = scheduler.schedule_one("Agent")
    assert second.accepted is True
    assert second.scheduled is True
    assert second.job is not None
    assert second.job.job_id != first_job_id
    assert second.job.task_id == TASK_ID
    assert [error.code for error in second.skipped] == []
    assert adapter.moves == []
    assert adapter.snapshot.tasks[TASK_ID].current_state == "Todo"


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


def test_scheduler_resumes_repair_ready_job_in_its_existing_workspace(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    workspace = tmp_path / "existing-workspace"
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000FIX",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(workspace),
        status="completion_rejected",
        metadata={"repair_ready": True, "repair_packet": "# Open Tulid Repair\n"},
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
    assert result.job.job_id == "01J00000000000000000000FIX"
    assert result.job.workspace_path == str(workspace)


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


def _failed_job_with_attempts(
    store: FileExecutionJobStore,
    *,
    job_id: str,
    revision: str,
    attempt_numbers: tuple[int, ...],
    metadata: Mapping[str, Any] | None = None,
) -> None:
    assert store.create(ExecutionJob(
        job_id=job_id,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(Path(job_id)),
        status="failed",
        attempts=max(attempt_numbers, default=0),
        metadata=dict(metadata or {}),
    )).accepted is True
    for number in attempt_numbers:
        assert store.record_attempt(job_id, attempt_record_to_dict(AttemptRecord(
            schema="tulid.attempt/v1",
            attempt_id=attempt_id_for(job_id, number),
            job_id=job_id,
            attempt_number=number,
            task_revision=revision,
            transition_id="implement",
            worker_id="codex",
            status="ended",
        ))).accepted is True


def test_scheduler_stops_mixed_fresh_and_repair_attempts_at_total_bound_and_survives_restart(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    revision = task_semantic_revision(_snapshot().tasks[TASK_ID])
    now = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    # Job A consumed one fresh attempt; Job B consumed one fresh + one repair
    # (attempts 2..N) before failing. Combined durable account = 3.
    _failed_job_with_attempts(
        store, job_id="01J00000000000000000000A00", revision=revision,
        attempt_numbers=(1,), metadata={"updated_at": now},
    )
    _failed_job_with_attempts(
        store, job_id="01J00000000000000000000B00", revision=revision,
        attempt_numbers=(1, 2), metadata={"updated_at": now},
    )

    def scheduler() -> Scheduler:
        return Scheduler(
            workflow=_workflow(),
            adapter=FakeAdapter(_snapshot()),
            job_store=store,
            workspace_root=tmp_path / "workspaces",
            failed_job_backoff_seconds=0,
            max_total_attempts_per_transition=3,
        )

    first = scheduler().schedule_one("Agent")
    assert first.accepted is True
    assert first.scheduled is False
    assert first.skipped[0].code == "job.total_attempt_limit_reached"

    # A fresh daemon/instance sharing the same store must not renew the account.
    second = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=FileExecutionJobStore(tmp_path / "jobs"),
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=0,
        max_total_attempts_per_transition=3,
    ).schedule_one("Agent")
    assert second.accepted is True
    assert second.scheduled is False
    assert second.skipped[0].code == "job.total_attempt_limit_reached"


def test_scheduler_allows_schedule_below_total_bound(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    revision = task_semantic_revision(_snapshot().tasks[TASK_ID])
    now = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    # Job A consumed one attempt; two remain within a total of three.
    _failed_job_with_attempts(
        store, job_id="01J00000000000000000000A00", revision=revision,
        attempt_numbers=(1,), metadata={"updated_at": now},
    )
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=0,
        max_total_attempts_per_transition=3,
        max_failed_attempts_per_transition=0,
    )
    result = scheduler.schedule_one("Agent")
    assert result.accepted is True
    assert result.scheduled is True
    assert result.job is not None


def test_scheduler_renews_budget_when_required_source_content_changes(tmp_path: Path):
    # A change to the task's required source content (its spec) is an explicit
    # new semantic revision: old attempts keep their original identity, so a
    # source decision change starts a fresh attempt budget rather than exhausting
    # the durable account.
    spec_dir = tmp_path / "artifacts"
    spec_dir.mkdir()
    spec = spec_dir / "spec.md"
    spec.write_text("Version one.\n", encoding="utf-8")

    task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
        artifact_links=("artifacts/spec.md",),
    )
    snapshot = _snapshot(task)
    transition = _workflow().transitions["implement"]

    def current_revision() -> str:
        return task_semantic_revision(
            task,
            source_identities=resolve_source_content_identities(
                project_root=tmp_path,
                task=task,
                transition=transition,
            ),
        )

    revision_one = current_revision()
    store = FileExecutionJobStore(tmp_path / "jobs")
    now = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _failed_job_with_attempts(
        store, job_id="01J00000000000000000000A00", revision=revision_one,
        attempt_numbers=(1, 2), metadata={"updated_at": now},
    )

    def scheduler() -> Scheduler:
        return Scheduler(
            workflow=_workflow(),
            adapter=FakeAdapter(snapshot),
            job_store=store,
            workspace_root=tmp_path / "workspaces",
            project_root=tmp_path,
            repo_root=tmp_path,
            failed_job_backoff_seconds=0,
            max_total_attempts_per_transition=2,
        )

    # Two attempts already consumed for revision one: no room within the bound of
    # two, so scheduling stops.
    exhausted = scheduler().schedule_one("Agent")
    assert exhausted.accepted is True
    assert exhausted.scheduled is False
    assert exhausted.skipped[0].code == "job.total_attempt_limit_reached"

    # The spec changes -> a new semantic revision -> the durable account is not
    # renewably exhausted; the task may be scheduled under a fresh budget.
    spec.write_text("Version two, changed.\n", encoding="utf-8")
    renewed = scheduler().schedule_one("Agent")
    assert renewed.accepted is True
    assert renewed.scheduled is True


def test_scheduler_stops_on_non_retryable_classified_failure(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    revision = task_semantic_revision(_snapshot().tasks[TASK_ID])
    now = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _failed_job_with_attempts(
        store, job_id="01J00000000000000000000A00", revision=revision,
        attempt_numbers=(1,),
        metadata={
            "updated_at": now,
            "failure_code": "worker.environment",
            "failure_category": "environment",
            "retryable": False,
            "retry_action": "stop; report the missing tool/dependency blocker",
            "failure_evidence": "command not found: python",
        },
    )
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=0,
    )
    result = scheduler.schedule_one("Agent")
    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.non_retryable_failure"
    assert "non-retryable cause (environment)" in result.skipped[0].message


def test_scheduler_retries_retryable_classified_failure(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    revision = task_semantic_revision(_snapshot().tasks[TASK_ID])
    now = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _failed_job_with_attempts(
        store, job_id="01J00000000000000000000A00", revision=revision,
        attempt_numbers=(1,),
        metadata={
            "updated_at": now,
            "failure_code": "provider.upstream",
            "failure_category": "provider",
            "retryable": True,
            "retry_action": "retry within the total attempt budget",
        },
    )
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=0,
    )
    result = scheduler.schedule_one("Agent")
    assert result.accepted is True
    assert result.scheduled is True


def test_scheduler_ignores_legacy_failed_job_without_attempt_records_for_cause_gate(tmp_path: Path):
    # A legacy failed job with no attempt records must not be mistaken for a
    # non-retryable cause: read history without inventing a blocked retry.
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000A00",
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
        adapter=FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        failed_job_backoff_seconds=0,
    )
    result = scheduler.schedule_one("Agent")
    assert result.accepted is True
    assert result.scheduled is True


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


def test_scheduler_blocks_concurrent_integration_across_projects_that_share_a_repository(tmp_path: Path):
    # Two configured projects can point at the same repository; their worker
    # model resources may differ, but they must not integrate concurrently.
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = repository_identity(repo)
    assert identity is not None
    task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    # An active integration job from ANOTHER project on the same repository.
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="OtherProject",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="running",
        metadata={"repository_identity": identity},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot(task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "repo_lane.active_job_exists"


def test_scheduler_allows_integration_when_other_project_uses_a_different_repository(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = repository_identity(repo)
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    assert repository_identity(other_repo) != identity
    task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="OtherProject",
        task_id=TASK_ID,
        transition_id="implement",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="running",
        metadata={"repository_identity": repository_identity(other_repo)},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot(task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == TASK_ID
    assert result.transition_id == "implement"


def test_scheduler_admits_dependent_against_accepted_repository_identity(tmp_path: Path):
    # Plan 5F: a dependent job is admitted against the accepted repository
    # identity of its dependency, not merely an updated board column.
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = repository_identity(repo)
    assert identity is not None
    dep = Task(
        id="01J00000000000000000000002",
        title="Dep",
        path="tasks/dep.md",
        current_state="Done",
        task_type="task",
    )
    dependent = Task(
        id=TASK_ID,
        title="Dependent",
        path="tasks/dependent.md",
        current_state="Todo",
        task_type="task",
        dependencies=("01J00000000000000000000002",),
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    # The dependency was accepted into this exact repository identity.
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id="01J00000000000000000000002",
        transition_id="review",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="accepted",
        metadata={"acceptance_repository_identity": identity},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(review=True),
        adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
    )

    _record_dependency_acceptance(tmp_path, store, dep)
    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == TASK_ID
    assert not any(skip.code.startswith("task.dependency") for skip in result.skipped)


def test_scheduler_rejects_dependent_admitted_on_board_move_without_acceptance(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dep = Task(
        id="01J00000000000000000000002",
        title="Dep",
        path="tasks/dep.md",
        current_state="Done",
        task_type="task",
    )
    dependent = Task(
        id=TASK_ID,
        title="Dependent",
        path="tasks/dependent.md",
        current_state="Todo",
        task_type="task",
        dependencies=("01J00000000000000000000002",),
    )
    # No ACCEPTED completion exists; the Done column alone is a board-only state.
    store = FileExecutionJobStore(tmp_path / "jobs")
    scheduler = Scheduler(
        workflow=_workflow(review=True),
        adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert [skip.code for skip in result.skipped].count("task.dependency_not_accepted") == 1


def test_scheduler_rejects_dependent_when_accepted_repository_identity_moved(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    stale = repository_identity(other)
    assert repository_identity(repo) != stale
    dep = Task(
        id="01J00000000000000000000002",
        title="Dep",
        path="tasks/dep.md",
        current_state="Done",
        task_type="task",
    )
    dependent = Task(
        id=TASK_ID,
        title="Dependent",
        path="tasks/dependent.md",
        current_state="Todo",
        task_type="task",
        dependencies=("01J00000000000000000000002",),
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id="01J00000000000000000000002",
        transition_id="review",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="accepted",
        metadata={"acceptance_repository_identity": stale},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(review=True),
        adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
    )

    _record_dependency_acceptance(tmp_path, store, dep)
    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert any(skip.code == "task.dependency_repo_moved" for skip in result.skipped)


def test_scheduler_keeps_repo_lane_unavailable_on_unresolved_acceptance(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = repository_identity(repo)
    assert identity is not None
    task = Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
    )
    journals = TransactionJournalStore(tmp_path / "events" / "journals")
    journals.prepare(
        journal_id="txn-unresolved",
        project_id="Agent",
        effects=({"type": "move_task", "task_id": TASK_ID, "to_state": "Review"},),
        events=(),
        task_id=TASK_ID,
        transition_id="implement",
        context={"repository_identity": identity},
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=FakeAdapter(_snapshot(task)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
        journal_store=journals,
    )

    result = scheduler.schedule_one("Agent")

    # The unresolved acceptance transaction owns the lane: schedule is refused
    # with an actionable reason until it commits or is reconciled.
    assert result.accepted is False
    assert result.errors[0].code == "repo_lane.unresolved_acceptance"


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


def test_scheduler_blocks_dependent_on_declared_failure_terminal(tmp_path: Path):
    # 6A: a dependency that finished in a declared failure terminal must not
    # admit a dependent, regardless of any accepted repository evidence.
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = repository_identity(repo)
    assert identity is not None
    dep = Task(
        id="01J00000000000000000000002",
        title="Dep",
        path="tasks/dep.md",
        current_state="Failed",
        task_type="task",
    )
    dependent = Task(
        id=TASK_ID,
        title="Dependent",
        path="tasks/dependent.md",
        current_state="Todo",
        task_type="task",
        dependencies=("01J00000000000000000000002",),
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id="01J00000000000000000000002",
        transition_id="review",
        worker_id="codex",
        workspace_path=str(tmp_path / "work"),
        status="accepted",
        metadata={"acceptance_repository_identity": identity},
    )).accepted is True
    states = {
        "Todo": StateDefinition(id="Todo"),
        "Review": StateDefinition(id="Review"),
        "Done": StateDefinition(id="Done", terminal_outcome="success"),
        "Failed": StateDefinition(id="Failed", terminal_outcome="failure"),
    }
    transitions = {
        "implement": TransitionDefinition(
            id="implement", task_type="task", from_state="Todo", to_state="Review",
            worker="codex", requires=RequirementDefinition(), transaction=None,
            default_for_scheduler=True,
        ),
        "review": TransitionDefinition(
            id="review", task_type="task", from_state="Review", to_state="Done",
            worker="codex", requires=RequirementDefinition(), transaction=None,
            default_for_scheduler=True,
        ),
    }
    workflow = WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType(states),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType(transitions),
    )
    scheduler = Scheduler(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
    )

    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is False
    assert any(skip.code == "task.dependency_failed" for skip in result.skipped)


def test_scheduler_uses_renamed_states_and_custom_task_types_for_success(tmp_path: Path):
    # 6A: success semantics are not hardcoded to the literal "Done"/"task" names;
    # arbitrary state names, a custom task type, and an arbitrary worker must
    # admit a dependent against declared success with accepted repo evidence.
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = repository_identity(repo)
    assert identity is not None
    dep = Task(
        id="01J00000000000000000000002",
        title="Dep",
        path="tasks/dep.md",
        current_state="Shipped",
        task_type="gadget",
    )
    dependent = Task(
        id=TASK_ID,
        title="Dependent",
        path="tasks/dependent.md",
        current_state="Open",
        task_type="gadget",
        dependencies=("01J00000000000000000000002",),
    )
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id="01J00000000000000000000002",
        transition_id="verify",
        worker_id="worker-7",
        workspace_path=str(tmp_path / "work"),
        status="accepted",
        metadata={"acceptance_repository_identity": identity},
    )).accepted is True
    states = {
        "Open": StateDefinition(id="Open"),
        "Verify": StateDefinition(id="Verify"),
        "Shipped": StateDefinition(id="Shipped", terminal_outcome="success"),
    }
    transitions = {
        "do": TransitionDefinition(
            id="do", task_type="gadget", from_state="Open", to_state="Verify",
            worker="worker-7", requires=RequirementDefinition(), transaction=None,
            default_for_scheduler=True,
        ),
        "verify": TransitionDefinition(
            id="verify", task_type="gadget", from_state="Verify", to_state="Shipped",
            worker="worker-7", requires=RequirementDefinition(), transaction=None,
            default_for_scheduler=True,
        ),
    }
    workflow = WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType(states),
        task_types=MappingProxyType({
            "gadget": TaskTypeDefinition(id="gadget", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType(transitions),
    )
    scheduler = Scheduler(
        workflow=workflow,
        adapter=FakeAdapter(_snapshot(dep, dependent)),
        job_store=store,
        workspace_root=tmp_path / "workspaces",
        project_root=tmp_path / "tracker",
        repo_root=repo,
    )

    _record_dependency_acceptance(tmp_path, store, dep)
    result = scheduler.schedule_one("Agent")

    assert result.accepted is True
    assert result.scheduled is True
    assert result.task_id == TASK_ID
    assert not any(skip.code.startswith("task.dependency") for skip in result.skipped)


def _record_dependency_acceptance(tmp_path, store, task, project_root=None):
    from open_tulid.runtime.attempts import task_semantic_revision
    job = store.list().jobs[0]
    journal_id = job.job_id + "-acceptance"
    journal_store = TransactionJournalStore((project_root or tmp_path / "tracker") / "events/journals")
    report = dict(job.metadata.get("verification_report") or {"checks": [{"status": "passed"}]})
    report.update(candidate_id="candidate", candidate_manifest_sha256="manifest")
    record = journal_store.prepare(journal_id=journal_id, project_id="Agent",
        task_id=task.id, transition_id=job.transition_id, effects=(), events=(), context={
            "job_id": job.job_id, "task_revision": task_semantic_revision(task),
            "verification_accepted": True, "expected_to_state": task.current_state,
            "verification_report": report, "candidate_id": "candidate", "candidate_manifest_sha256": "manifest",
        }).record
    assert journal_store.commit(record).accepted
    assert store.update_status(job.job_id, "accepted", metadata={
        "acceptance_transaction_id": journal_id,
        "verification_report": report,
        "review_result": {"behavior": "dependency", "evidence": "test", "remaining_blockers": []},
    }).accepted
