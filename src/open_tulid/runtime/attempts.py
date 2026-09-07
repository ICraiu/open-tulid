"""Versioned attempt records and the semantic task revision they reference.

Step 1A (reliability plan 1) defines a durable, versioned attempt record that
distinguishes an admitted attempt whose launch was interrupted from a worker
that actually ran. This module owns that record shape plus the semantic task
revision identity: the revision must be derived only from what the task
requires, excluding board state, timestamps, and generated audit links so a
restart, a different worker assignment, or a logging-only edit never renews the
retry budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from open_tulid.domain import ExecutionJob, Task

SEMANTIC_TASK_REVISION_SCHEMA = "tulid.task-revision/v1"
ATTEMPT_RECORD_SCHEMA = "tulid.attempt/v1"

# The semantic body sections that carry meaning for task requirements. Board
# location, current workflow state, generated audit links, and timestamps are
# deliberately excluded from the revision.
SEMANTIC_BODY_HEADINGS = ("why", "what", "how", "acceptance")

ATTEMPT_RECORD_METADATA_KEY = "attempt_records"


class AttemptStatus(str, Enum):
    """Lifecycle of one worker attempt.

    ``admitted`` is persisted under the store/lease coordination before the
    worker is spawned. An admitted attempt that never becomes ``running`` was
    launched but interrupted before a worker actually ran; a ``running``
    attempt that never reaches ``ended`` ran but was orphaned. Both must be
    distinguishable on restart without creating a new budget identity.
    """

    ADMITTED = "admitted"
    RUNNING = "running"
    ENDED = "ended"


@dataclass(frozen=True)
class AttemptRecord:
    """One versioned attempt of worker execution for a task transition.

    ``attempt_number`` is the 1-based count of worker processes started for the
    same job/task (a fresh process attempt, including a repair process).
    ``predecessor`` references the immediately preceding attempt identity so
    recovery can order attempts even across a job/job restart.
    """

    schema: str
    attempt_id: str
    job_id: str
    attempt_number: int
    task_revision: str
    transition_id: str
    worker_id: str
    predecessor: str | None = None
    status: AttemptStatus | str = AttemptStatus.ADMITTED
    started_at: str | None = None
    deadline: str | None = None
    ended_at: str | None = None
    failure_reference: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def status_value(self) -> str:
        return self.status.value if isinstance(self.status, AttemptStatus) else str(self.status)


def task_semantic_revision(task: Task) -> str:
    """A stable semantic revision for a task's required work.

    The revision is derived from the task type, dependency identities, parent
    identity, the concrete-outcome title, and the meaningful body sections. It
    excludes the task ``id``/``path`` (board location), ``current_state``
    (workflow state), ``artifact_links`` (generated audit links), and
    ``metadata`` (timestamps and machine bookkeeping), so those changes do not
    renew the retry budget.
    """
    outcome_title = _first_h1_title(task.body) or task.title
    payload = {
        "schema": SEMANTIC_TASK_REVISION_SCHEMA,
        "task_type": task.task_type,
        "title": outcome_title,
        "parent_id": task.parent_id,
        "dependencies": sorted(task.dependencies),
        "body": _semantic_body_sections(task.body),
    }
    return _canonical_sha256(payload)


def attempt_id_for(job_id: str, attempt_number: int) -> str:
    """Stable attempt identity derived from job identity and attempt number."""
    return f"{job_id}@{attempt_number}"


def attempt_deadline(
    *,
    started_at: datetime | None = None,
    attempt_duration_seconds: float,
    settlement_allowance_seconds: float = 0.0,
    now: datetime | None = None,
) -> str:
    """Wall-clock deadline for one managed attempt.

    The deadline is the explicit expiry used to bound the attempt and (via the
    per-attempt credential) the model session it issues. It equals the attempt
    start plus the permitted attempt duration plus the configured
    completion-settlement allowance.
    """
    start = started_at or (now or datetime.now(timezone.utc))
    allowed = max(0.0, attempt_duration_seconds) + max(0.0, settlement_allowance_seconds)
    return (start + timedelta(seconds=allowed)).isoformat()


def attempt_record_to_dict(record: AttemptRecord) -> dict[str, Any]:
    return {
        "schema": record.schema,
        "attempt_id": record.attempt_id,
        "job_id": record.job_id,
        "attempt_number": record.attempt_number,
        "task_revision": record.task_revision,
        "transition_id": record.transition_id,
        "worker_id": record.worker_id,
        "predecessor": record.predecessor,
        "status": record.status_value,
        "started_at": record.started_at,
        "deadline": record.deadline,
        "ended_at": record.ended_at,
        "failure_reference": record.failure_reference,
        "metadata": dict(record.metadata),
    }


def attempt_record_from_dict(payload: Mapping[str, Any]) -> AttemptRecord:
    schema = payload.get("schema", ATTEMPT_RECORD_SCHEMA)
    raw_status = payload.get("status", AttemptStatus.ADMITTED.value)
    status: AttemptStatus | str
    try:
        status = AttemptStatus(raw_status)
    except ValueError:
        status = raw_status
    return AttemptRecord(
        schema=str(schema),
        attempt_id=_required_string(payload, "attempt_id"),
        job_id=_required_string(payload, "job_id"),
        attempt_number=int(payload.get("attempt_number", 0)),
        task_revision=_required_string(payload, "task_revision"),
        transition_id=_required_string(payload, "transition_id"),
        worker_id=_required_string(payload, "worker_id"),
        predecessor=_optional_string(payload.get("predecessor")),
        status=status,
        started_at=_optional_string(payload.get("started_at")),
        deadline=_optional_string(payload.get("deadline")),
        ended_at=_optional_string(payload.get("ended_at")),
        failure_reference=_optional_string(payload.get("failure_reference")),
        metadata=dict(payload.get("metadata") or {}),
    )


def attempt_records_from_metadata(metadata: Mapping[str, Any]) -> tuple[AttemptRecord, ...]:
    """Load parsed attempt records from job metadata, tolerating missing/empty."""
    raw = metadata.get(ATTEMPT_RECORD_METADATA_KEY)
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ValueError("job metadata attempt_records must be a list")
    records = tuple(attempt_record_from_dict(item) for item in raw)
    return tuple(sorted(records, key=lambda record: record.attempt_number))


def count_consumed_attempts(
    *,
    jobs: Iterable[ExecutionJob],
    task_id: str,
    transition_id: str,
    task_revision: str,
    project_id: str | None = None,
) -> int:
    """Durable count of worker attempts already consumed for a task revision.

    Every admitted worker process — a fresh scheduler job or an in-place
    repair — persists a versioned attempt record under the store/lease
    coordination. This counts those records across all jobs for the same task
    and transition whose semantic task revision matches ``task_revision``.

    It deliberately ignores job creation times and the current runtime session,
    so a daemon restart cannot renew the total account. Jobs with no attempt
    records (legacy jobs, or a task that has since been re-authored) contribute
    nothing: the plan reads legacy history without inventing precise attempt
    counts.
    """
    total = 0
    for job in jobs:
        if job.task_id != task_id or job.transition_id != transition_id:
            continue
        if project_id is not None and job.project_id != project_id:
            continue
        try:
            records = attempt_records_from_metadata(job.metadata)
        except ValueError:
            continue
        total += sum(1 for record in records if record.task_revision == task_revision)
    return total


def _semantic_body_sections(body: str) -> dict[str, str]:
    """Extract the semantic sections (Why/What/How/Acceptance) from a task body."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            if current is not None:
                sections[current] = _clean_section(buffer)
            name = stripped[3:].strip().lower()
            current = name if name in SEMANTIC_BODY_HEADINGS else None
            buffer = []
            continue
        if stripped.startswith("# "):
            if current is not None:
                sections[current] = _clean_section(buffer)
            current = None
            buffer = []
            continue
        if current is not None:
            buffer.append(stripped)
    if current is not None:
        sections[current] = _clean_section(buffer)
    return sections


def _first_h1_title(body: str) -> str | None:
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _clean_section(lines: list[str]) -> str:
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"attempt field {key!r} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
