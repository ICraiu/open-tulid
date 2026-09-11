"""R5 — close durable revision and historical-attempt ambiguity.

This module demonstrates the conservative accounting policy for legacy jobs
without frozen inputs or explicit attempt records, and proves that the semantic
task revision includes every behavior-binding requirement, not only the
familiar Why/What/How/Acceptance headings.

Confirmed gaps closed here:
1. ``count_consumed_attempts`` previously treated a legacy, non-pending job
   with no attempt records and no frozen input identity as consuming **zero**
   worker executions. That lets a started job silently replenish a consumed
   budget. It now conservatively accounts such a job (at least one execution)
   and records an explicit diagnostic naming the job.
2. ``task_semantic_revision`` previously derived the revision only from
   Why/What/How/Acceptance. A behavior-binding requirement expressed in any
   other ``## `` section (Constraints, Interface, Non-goals, ...) is now part
   of the revision, so editing it creates a new attempt budget while generated
   history (board state, path, audit links, metadata) still does not.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
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
from open_tulid.runtime import (
    AttemptRecord,
    AttemptStatus,
    ATTEMPT_RECORD_METADATA_KEY,
    FileExecutionJobStore,
    Scheduler,
    attempt_id_for,
    attempt_record_to_dict,
    attempt_records_from_metadata,
    count_consumed_attempts,
    task_semantic_revision,
)
TASK_ID = "01J00000000000000000000001"
JOB_ID = "01J00000000000000000000JOB"

BASE_BODY = (
    "# Implement thing\n\n"
    "Do the work.\n\n"
    "## Why\nbecause.\n"
    "## What\ndo x.\n"
    "## How\nstep 1.\n"
    "## Acceptance\nworks\n"
    "## Constraints\nmust not use eval.\n"
    "## Non-goals\nno CLI expansion.\n"
)


def _task(body: str = BASE_BODY) -> Task:
    return Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
        dependencies=(),
        body=body,
    )


def _record(
    job_id: str,
    number: int,
    *,
    revision: str,
    status: str = "ended",
    transition_id: str = "code",
) -> dict[str, Any]:
    return attempt_record_to_dict(AttemptRecord(
        schema="tulid.attempt/v1",
        attempt_id=attempt_id_for(job_id, number),
        job_id=job_id,
        attempt_number=number,
        task_revision=revision,
        transition_id=transition_id,
        worker_id="codex",
        predecessor=attempt_id_for(job_id, number - 1) if number > 1 else None,
        status=status,
    ))


def _workflow() -> WorkflowDefinition:
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
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="Review",
                worker="codex",
                requires=RequirementDefinition(),
                transaction=None,
                default_for_scheduler=True,
            ),
        }),
    )


def _snapshot(*tasks: Task) -> ProjectSnapshot:
    if not tasks:
        tasks = (_task(),)
    return ProjectSnapshot(
        project_id="Agent",
        tasks=MappingProxyType({task.id: task for task in tasks}),
        board_positions=MappingProxyType({
            task.id: BoardPosition(board="Work", column=task.current_state, card_text=task.title, line=index)
            for index, task in enumerate(tasks, start=1)
        }),
    )


class _FakeAdapter:
    capabilities: frozenset[AdapterCapability] = frozenset({AdapterCapability.LOAD_PROJECT})

    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot

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


# ---------------------------------------------------------------------------
# Semantic revision must include every binding section
# ---------------------------------------------------------------------------


def test_editing_binding_requirement_outside_why_what_how_changes_revision():
    base = _task()
    constraint_changed = _task(body=BASE_BODY.replace("must not use eval", "must use a sandbox"))
    appended_binding = _task(body=BASE_BODY + "## Interface\npublishes `POST /runs`.\n")
    removed_binding = _task(body=BASE_BODY.replace(
        "## Non-goals\nno CLI expansion.\n", ""
    ))
    assert task_semantic_revision(base) != task_semantic_revision(constraint_changed)
    assert task_semantic_revision(base) != task_semantic_revision(appended_binding)
    assert task_semantic_revision(base) != task_semantic_revision(removed_binding)


def test_editing_generated_history_does_not_change_revision():
    task = _task()
    moved = Task(
        id="different-id",
        title=task.title,
        path="tasks/other.md",
        current_state="Review",
        task_type=task.task_type,
        dependencies=(),
        artifact_links=("artifacts/audit/1.md",),
        parent_id=task.parent_id,
        metadata={"created_at": "2026-09-07T00:00:00+00:00"},
        body=task.body,
    )
    assert task_semantic_revision(task) == task_semantic_revision(moved)


# ---------------------------------------------------------------------------
# Historical-shape accounting matrix
# ---------------------------------------------------------------------------


def test_legacy_pending_job_without_records_is_proven_never_launched(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID, project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex",
        workspace_path=str(tmp_path / "w"),
        status="pending", attempts=0,
    )).accepted is True
    assert count_consumed_attempts(
        jobs=store.list().jobs, task_id=TASK_ID, transition_id="code",
        task_revision=task_semantic_revision(_task()),
    ) == 0


def test_legacy_started_job_without_records_is_conservatively_accounted_with_diagnostic(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID, project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex",
        workspace_path=str(tmp_path / "w"),
        status="failed", attempts=0,
    )).accepted is True
    diagnostics: list[str] = []
    consumed = count_consumed_attempts(
        jobs=store.list().jobs, task_id=TASK_ID, transition_id="code",
        task_revision=task_semantic_revision(_task()),
        diagnostics=diagnostics,
    )
    # Never assume zero merely because the modern attempt field is absent.
    assert consumed == 1
    assert any(JOB_ID in diagnostic for diagnostic in diagnostics)


def test_legacy_started_job_with_attempts_counts_attempts_not_just_one(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID, project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex",
        workspace_path=str(tmp_path / "w"),
        status="failed", attempts=3,
    )).accepted is True
    consumed = count_consumed_attempts(
        jobs=store.list().jobs, task_id=TASK_ID, transition_id="code",
        task_revision=task_semantic_revision(_task()),
    )
    assert consumed == 3


def test_modern_reservation_only_and_interrupted_launch_counts_durable_admission(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    revision = task_semantic_revision(_task())
    assert store.create(ExecutionJob(
        job_id=JOB_ID, project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex",
        workspace_path=str(tmp_path / "w"),
        status="pending", attempts=0,
    )).accepted is True
    # Reservation-only (never admitted): still 0.
    assert count_consumed_attempts(
        jobs=store.list().jobs, task_id=TASK_ID, transition_id="code",
        task_revision=revision,
    ) == 0
    # A durable ADMITTED attempt (launch interrupted) is persisted only once a
    # worker admission is written; it consumes one admission.
    assert store.record_attempt(JOB_ID, _record(
        JOB_ID, 1, revision=revision, status=AttemptStatus.ADMITTED.value
    )).accepted is True
    assert store.record_attempt(JOB_ID, _record(
        JOB_ID, 2, revision=revision, status=AttemptStatus.RUNNING.value
    )).accepted is True
    assert count_consumed_attempts(
        jobs=store.list().jobs, task_id=TASK_ID, transition_id="code",
        task_revision=revision,
    ) == 2


def test_unreadable_attempt_record_stops_accounting_with_record_identified(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID, project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex",
        workspace_path=str(tmp_path / "w"),
        status="failed",
        metadata={"attempt_records": "corrupt"},
    )).accepted is True
    try:
        count_consumed_attempts(
            jobs=store.list().jobs, task_id=TASK_ID, transition_id="code",
            task_revision=task_semantic_revision(_task()),
        )
    except ValueError as exc:
        assert JOB_ID in str(exc)
    else:
        raise AssertionError("unreadable attempt record must stop accounting")


# ---------------------------------------------------------------------------
# Scheduler admission must not replenish
# ---------------------------------------------------------------------------


def test_scheduler_blocks_when_legacy_started_job_exhausts_finite_budget(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID, project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex",
        workspace_path=str(tmp_path / "w"),
        status="failed", attempts=1,
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=_FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "ws",
        failed_job_backoff_seconds=0,
        max_total_attempts_per_transition=1,
    )
    result = scheduler.schedule_one("Agent")
    # The legacy started job is conservatively accounted as 1, which equals the
    # bounded budget: scheduling must block, not silently admit and replenish.
    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.total_attempt_limit_reached"


def test_scheduler_surfaces_unreadable_history_as_attempt_history_unreadable(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID, project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex",
        workspace_path=str(tmp_path / "w"),
        status="failed",
        metadata={"attempt_records": "corrupt"},
    )).accepted is True
    scheduler = Scheduler(
        workflow=_workflow(),
        adapter=_FakeAdapter(_snapshot()),
        job_store=store,
        workspace_root=tmp_path / "ws",
        failed_job_backoff_seconds=0,
        max_total_attempts_per_transition=3,
    )
    result = scheduler.schedule_one("Agent")
    assert result.accepted is True
    assert result.scheduled is False
    assert result.skipped[0].code == "job.attempt_history_unreadable"
    assert JOB_ID in result.skipped[0].message


# ---------------------------------------------------------------------------
# Restart chain with a finite exhausted budget
# ---------------------------------------------------------------------------

def _build_exhausted_store(root: Path) -> tuple[Path, str]:
    store = FileExecutionJobStore(root)
    revision = task_semantic_revision(_task())
    # Modern exhausted job: three durable ended attempts at the bound.
    assert store.create(ExecutionJob(
        job_id=f"J-modern", project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex", workspace_path=str(root / "m"),
        status="failed", attempts=3,
    )).accepted is True
    for number in (1, 2, 3):
        assert store.record_attempt(
            f"J-modern", _record(f"J-modern", number, revision=revision)
        ).accepted is True
    # Legacy started job: no records, conservatively accounted as 1.
    assert store.create(ExecutionJob(
        job_id=f"J-legacy-started", project_id="Agent", task_id=TASK_ID,
        transition_id="code", worker_id="codex", workspace_path=str(root / "l"),
        status="failed", attempts=1,
    )).accepted is True
    # Legacy never-launched job: pending, no records, consumes nothing. It is
    # tested separately because a pending job legitimately holds the repo lane
    # as an active job, which would mask the total-limit gate's own decision.
    return root, revision


def test_restart_chain_with_finite_exhausted_budget_does_not_replenish(tmp_path: Path):
    store_root, revision = _build_exhausted_store(tmp_path / "jobs")

    def consumed_count(root: Path) -> int:
        return count_consumed_attempts(
            jobs=FileExecutionJobStore(root).list().jobs,
            task_id=TASK_ID, transition_id="code", task_revision=revision,
        )

    # 3 modern + 1 conservative legacy started = 4.
    assert consumed_count(store_root) == 4

    def tail():
        scheduler = Scheduler(
            workflow=_workflow(),
            adapter=_FakeAdapter(_snapshot()),
            job_store=FileExecutionJobStore(store_root),
            workspace_root=tmp_path / "ws",
            failed_job_backoff_seconds=0,
            max_total_attempts_per_transition=4,
        )
        return scheduler.schedule_one("Agent")

    # A daemon restart reopens the store; the same finite account still blocks.
    for _ in range(3):
        result = tail()
        assert result.accepted is True
        assert result.scheduled is False
        assert result.skipped[0].code == "job.total_attempt_limit_reached"

    # Evidence remains preserved across every restart.
    recovered = FileExecutionJobStore(store_root).get("J-modern")
    assert recovered.job is not None
    records = attempt_records_from_metadata(recovered.job.metadata)
    assert [record.attempt_number for record in records] == [1, 2, 3]
    assert all(record.status_value == "ended" for record in records)


def test_concurrent_admission_charges_exactly_one_job_and_preserves_evidence(tmp_path: Path):
    root = tmp_path / "jobs"
    revision = task_semantic_revision(_task())

    def scheduler_runs(self_scheduler: Scheduler) -> bool:
        result = self_scheduler.schedule_one("Agent")
        return result.accepted and result.scheduled

    barrier = Barrier(2)

    def try_schedule(index: int) -> bool:
        own = FileExecutionJobStore(root)
        sched = Scheduler(
            workflow=_workflow(),
            adapter=_FakeAdapter(_snapshot()),
            job_store=own,
            workspace_root=tmp_path / "ws",
        )
        barrier.wait()
        return scheduler_runs(sched)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(try_schedule, [0, 1]))
    # Exactly one of the two racing schedulers admits a job.
    assert outcomes.count(True) == 1

    store = FileExecutionJobStore(root)
    listed = store.list()
    assert listed.accepted is True
    active = [job for job in listed.jobs
              if str(getattr(job.status, "value", job.status)) in {
                  "pending", "running", "completion_submitted",
              }]
    assert len(active) == 1
    # Nothing was pre-counted before launch; admission is durable and unique.
    assert len(active[0].metadata.get(ATTEMPT_RECORD_METADATA_KEY, ())) == 0
