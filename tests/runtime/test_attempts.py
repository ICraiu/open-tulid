from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from open_tulid.models import ProjectConfig, RuntimeConfig
from open_tulid.runtime import (
    AttemptRecord,
    AttemptStatus,
    ATTEMPT_RECORD_METADATA_KEY,
    FileExecutionJobStore,
    JobExecutor,
    JsonlEventStore,
    RuntimeBaseline,
    attempt_deadline,
    attempt_id_for,
    attempt_record_from_dict,
    attempt_record_to_dict,
    attempt_records_from_metadata,
    baseline_to_dict,
    capture_runtime_baseline,
    task_semantic_revision,
    workflow_sha256,
    write_runtime_baseline,
)
from open_tulid.containers.runtime import AgentRunResult

TASK_ID = "01J00000000000000000000001"
JOB_ID = "01J00000000000000000000JOB"


def _task(body: str = "# Implement thing\n\nDo work.\n\n## Why\nbecause.\n"
                      "## What\ndo x.\n## How\nstep 1.\n## Acceptance\nworks") -> Task:
    return Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
        dependencies=("b", "a"),
        body=body,
    )


def _record(attempt_number: int = 1, *, status: str = "admitted") -> AttemptRecord:
    return AttemptRecord(
        schema="tulid.attempt/v1",
        attempt_id=attempt_id_for(JOB_ID, attempt_number),
        job_id=JOB_ID,
        attempt_number=attempt_number,
        task_revision="rev-1",
        transition_id="code",
        worker_id="codex",
        predecessor=attempt_id_for(JOB_ID, attempt_number - 1) if attempt_number > 1 else None,
        status=status,
        started_at="2026-09-07T10:00:00+00:00",
        deadline="2026-09-07T12:00:00+00:00",
    )


def _job() -> ExecutionJob:
    return ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(Path("/tmp") / "work"),
    )


def test_semantic_revision_ignores_board_state_timestamps_and_audit_links():
    # id, path (board location), current_state (workflow state), metadata
    # (timestamps/machine bookkeeping), and artifact_links (audit links) must
    # not change the semantic revision.
    task = _task()
    relocated = Task(
        id="different-id",
        title=task.title,
        path="tasks/other.md",
        current_state="Review",
        task_type=task.task_type,
        dependencies=("a", "b"),
        artifact_links=("artifacts/audit/1.md",),
        parent_id=task.parent_id,
        metadata={"created_at": "2026-09-07T00:00:00+00:00"},
        body=task.body,
    )
    assert task_semantic_revision(task) == task_semantic_revision(relocated)


def test_semantic_revision_normalizes_dependency_order():
    a = _task()
    reversed_order = Task(
        id=a.id, title=a.title, path=a.path, current_state=a.current_state,
        task_type=a.task_type, dependencies=("a", "b"), body=a.body,
    )
    assert task_semantic_revision(a) == task_semantic_revision(reversed_order)


def test_semantic_revision_changes_when_requirements_change():
    base = _task()
    changed = _task(body="# Implement thing\n\nDo work.\n\n## Why\nbecause.\n"
                      "## What\ndo CHANGED.\n## How\nstep 1.\n## Acceptance\nworks")
    assert task_semantic_revision(base) != task_semantic_revision(changed)


def test_semantic_revision_changes_on_dependency_change():
    a = _task()
    b = _task()
    base = Task(
        id=TASK_ID, title=a.title, path=a.path, current_state=a.current_state,
        task_type=a.task_type, dependencies=("b", "a"), body=a.body,
    )
    changed = Task(
        id=TASK_ID, title=a.title, path=a.path, current_state=a.current_state,
        task_type=a.task_type, dependencies=("c",), body=a.body,
    )
    assert task_semantic_revision(base) != task_semantic_revision(changed)


def test_attempt_record_round_trips_through_dict():
    payload = attempt_record_to_dict(_record(status="ended"))
    payload["ended_at"] = "2026-09-07T12:00:00+00:00"
    record = attempt_record_from_dict(payload)
    restored = attempt_record_from_dict(attempt_record_to_dict(record))
    assert restored == record
    assert restored.status_value == "ended"
    assert restored.ended_at == "2026-09-07T12:00:00+00:00"


def test_attempt_deadline_includes_duration_and_settlement_allowance():
    now = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
    deadline = attempt_deadline(
        started_at=now,
        attempt_duration_seconds=7200,
        settlement_allowance_seconds=1800,
    )
    expected = now + timedelta(seconds=7200 + 1800)
    assert deadline == expected.isoformat()


def test_store_persists_attempt_record_across_restart(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(_job()).accepted is True
    assert store.record_attempt(JOB_ID, attempt_record_to_dict(_record(1))).accepted is True

    reloaded = FileExecutionJobStore(tmp_path / "jobs").get(JOB_ID)
    assert reloaded.job is not None
    records = attempt_records_from_metadata(reloaded.job.metadata)
    assert len(records) == 1
    assert records[0].attempt_number == 1
    assert records[0].status_value == "admitted"
    assert records[0].job_id == JOB_ID


def test_store_upserts_attempt_by_attempt_id(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(_job()).accepted is True
    assert store.record_attempt(JOB_ID, attempt_record_to_dict(_record(1))).accepted is True
    assert store.record_attempt(JOB_ID, attempt_record_to_dict(_record(2))).accepted is True
    moved = attempt_record_to_dict(
        _record(1, status="running")
    )
    assert store.record_attempt(JOB_ID, moved).accepted is True

    records = attempt_records_from_metadata(store.get(JOB_ID).job.metadata)
    assert [record.attempt_number for record in records] == [1, 2]
    assert records[0].status_value == "running"
    assert len(records) == 2


def test_store_tracks_admitted_running_ended_distinction(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(_job()).accepted is True
    admitted = _record(1, status="admitted")
    assert store.record_attempt(JOB_ID, attempt_record_to_dict(admitted)).accepted is True
    running = _record(1, status="running")
    assert store.record_attempt(JOB_ID, attempt_record_to_dict(running)).accepted is True
    ended = _record(1, status="ended", )
    ended_payload = attempt_record_to_dict(ended)
    ended_payload["ended_at"] = "2026-09-07T12:00:00+00:00"
    ended_payload["failure_reference"] = JOB_ID
    assert store.record_attempt(JOB_ID, ended_payload).accepted is True

    record = attempt_records_from_metadata(store.get(JOB_ID).job.metadata)[0]
    assert record.status_value == "ended"
    assert record.ended_at == "2026-09-07T12:00:00+00:00"
    assert record.failure_reference == JOB_ID


def test_attempt_predecessor_links_same_job_chain():
    second = _record(2)
    assert second.predecessor == attempt_id_for(JOB_ID, 1)
    first = _record(1)
    assert first.predecessor is None


def test_job_attempts_counter_unchanged_by_record_attempt(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(_job()).accepted is True
    assert store.record_attempt(JOB_ID, attempt_record_to_dict(_record(1))).accepted is True
    # Recording an attempt must never mutate the budget counter (job.attempts).
    assert store.get(JOB_ID).job.attempts == 0


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "Review": StateDefinition(id="Review"),
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
                to_state="Review",
                worker="codex",
                requires=RequirementDefinition(),
                transaction=None,
            ),
        }),
    )


def test_capture_runtime_baseline_records_source_and_worker_identity(tmp_path: Path):
    source = tmp_path / "repo"
    source.mkdir()
    workflow = _workflow()
    baseline = capture_runtime_baseline(
        source_root=source,
        workflow=workflow,
        worker_id="codex",
        worker_images={"codex": "open-tulid/agent:latest"},
        worker_types={"codex": "docker"},
        worker_args={"codex": ("exec", "{prompt_packet}")},
        default_timeout_seconds=7200,
        max_repair_attempts=2,
        command_policy_sha256="policy-hash",
    )

    assert baseline.schema == "tulid.runtime-baseline/v1"
    assert baseline.worker_id == "codex"
    assert baseline.worker_image_identity == "open-tulid/agent:latest"
    assert baseline.command_policy_hash == "policy-hash"
    assert baseline.workflow_hash == workflow_sha256(workflow)
    assert baseline.sha256
    assert baseline.effective_worker_config["worker_id"] == "codex"
    assert baseline.effective_worker_config["worker_args"] == ("exec", "{prompt_packet}")


def test_capture_runtime_baseline_without_git_is_low_failure(tmp_path: Path):
    source = tmp_path / "repo"
    source.mkdir()
    baseline = capture_runtime_baseline(
        source_root=source,
        workflow=_workflow(),
        worker_id="codex",
    )
    assert baseline.source_revision is None
    assert baseline.source_dirty is None
    assert baseline.dirty_patch_identity is None
    assert baseline.worker_image_identity is None


def test_write_runtime_baseline_writes_isolated_fixture(tmp_path: Path):
    workspace = tmp_path / "workspace"
    baseline = capture_runtime_baseline(
        source_root=None,
        workflow=_workflow(),
        worker_id="codex",
    )
    path = write_runtime_baseline(baseline, workspace)

    assert path.is_file()
    assert path.name == "runtime-baseline.json"
    assert path.parent == workspace / ".open-tulid"


def test_baseline_from_dict_round_trip(tmp_path: Path):
    baseline = capture_runtime_baseline(
        source_root=None,
        workflow=_workflow(),
        worker_id="codex",
        worker_images={"codex": "img"},
    )
    restored = RuntimeBaseline(**baseline_to_dict(baseline))
    restored = type(baseline)(
        schema=baseline.schema,
        source_root=baseline.source_root,
        source_revision=baseline.source_revision,
        source_dirty=baseline.source_dirty,
        dirty_patch_identity=baseline.dirty_patch_identity,
        installed_tulid_location=baseline.installed_tulid_location,
        installed_tulid_revision=baseline.installed_tulid_revision,
        workflow_hash=baseline.workflow_hash,
        command_policy_hash=baseline.command_policy_hash,
        worker_image_identity=baseline.worker_image_identity,
        worker_id=baseline.worker_id,
        effective_worker_config=baseline.effective_worker_config,
        sha256=baseline.sha256,
    )
    assert baseline_to_dict(restored) == baseline_to_dict(baseline)


class _FakeCompletionEndpoint:
    job_id: str
    host: str = "127.0.0.1"
    port: int = 0

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:0/jobs/{self.job_id}/complete"

    def stop(self) -> None:
        return None


class _ExecAdapter:
    capabilities: frozenset[AdapterCapability] = frozenset({
        AdapterCapability.LOAD_PROJECT,
        AdapterCapability.READ_TASK,
    })

    def load_project(self) -> LoadProjectResult:
        return LoadProjectResult(snapshot=_snapshot())

    def read_task(self, task_id: str) -> ReadTaskResult:
        return ReadTaskResult(task=_task()) if task_id == TASK_ID else ReadTaskResult()

    def write_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        return WriteResult(path=state)

    def append_event(self, event: Mapping[str, Any]) -> WriteResult:
        return WriteResult(path="events/test.jsonl")


def _snapshot() -> ProjectSnapshot:
    task = _task()
    return ProjectSnapshot(
        project_id="Agent",
        tasks=MappingProxyType({task.id: task}),
        board_positions=MappingProxyType({
            task.id: BoardPosition(board="Work", column="Todo", card_text=task.title, line=1),
        }),
    )


def test_executor_admits_and_settles_attempt_with_baseline(
    tmp_path: Path,
    monkeypatch,
):
    """The executor persists a versioned attempt admission before the worker
    spawn and settles it afterwards, recording the isolated baseline in the
    workspace and on the job."""
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    job = ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
    )
    assert store.create(job).accepted is True

    def fake_execute(request, *, docker_executable):
        output = Path(request.workspace) / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.md").write_text("done\n", encoding="utf-8")
        # Accept the completion (the real worker would submit it over HTTP; we
        # settle it directly to keep the workspace intact for assertions).
        assert store.update_status(JOB_ID, "accepted").accepted is True
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=0,
        )

    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        fake_execute,
    )
    monkeypatch.setattr(
        "open_tulid.runtime.executor.JobExecutor._start_completion_endpoint",
        lambda self, job_id: _FakeCompletionEndpoint(job_id),
    )

    executor = JobExecutor(
        workflow=_workflow(),
        adapter=_ExecAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_args={"codex": ("exec", "{prompt_packet}")},
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "accepted"
    records = attempt_records_from_metadata(loaded.job.metadata)
    assert len(records) == 1
    record = records[0]
    assert record.attempt_number == 1
    assert record.attempt_id == attempt_id_for(JOB_ID, 1)
    assert record.task_revision == task_semantic_revision(_task())
    assert record.transition_id == "code"
    assert record.worker_id == "codex"
    assert record.predecessor is None
    assert record.status_value == "ended"
    assert record.deadline is not None
    assert record.failure_reference is None

    baseline_file = workspace / ".open-tulid" / "runtime-baseline.json"
    assert baseline_file.is_file()
    assert record.metadata["baseline"]["worker_id"] == "codex"
    assert record.metadata["baseline"]["worker_image_identity"] is None
