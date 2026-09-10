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
from dataclasses import dataclass, field, replace as _replace_record
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from open_tulid.domain import ExecutionJob, Task

SEMANTIC_TASK_REVISION_SCHEMA = "tulid.task-revision/v2"
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


def task_semantic_revision(
    task: Task,
    *,
    source_identities: Iterable[tuple[str, str]] = (),
) -> str:
    """A stable semantic revision for a task's required work.

    The revision is derived from the task type, dependency identities, parent
    identity, the concrete-outcome title, the meaningful body sections, and the
    identities (original reference plus content digest) of the selected required
    source content the task depends on: its specification, settled canonical
    answers, and any binding generated contract. If that source content changes,
    the revision is an explicit new identity, so old attempts keep their original
    revision and remain inspectable while a re-authored task starts a fresh
    attempt budget.

    It excludes the task ``id``/``path`` (board location), ``current_state``
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
    normalized = _normalized_source_identities(source_identities)
    if normalized:
        # Required source content influences the revision only when such content
        # is selected. This also keeps the no-source path byte-identical with the
        # pre-3B revision so legacy attempts keep their original identity.
        payload["source_content"] = normalized
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


def reconcile_attempt_records(
    records: Iterable[AttemptRecord],
    *,
    ended_at: str | None = None,
    failure_reference: str | None = None,
) -> tuple[AttemptRecord, ...]:
    """Mark incomplete attempt records ended for restart reconciliation.

    On a restart, an attempt that was only admitted (launch interrupted) or was
    running (worker orphaned) never reached an explicit ``ended`` state. This
    settles each such record while preserving its durable admission, so restart
    recovery starts a bounded fresh attempt without renewing the total budget or
    pretending the worker ran to completion.

    Already-ended records are returned unchanged.
    """
    incomplete = {
        AttemptStatus.ADMITTED.value,
        AttemptStatus.RUNNING.value,
    }
    reconciled: list[AttemptRecord] = []
    for record in records:
        if record.status_value not in incomplete:
            reconciled.append(record)
            continue
        reconciled.append(_replace_record(
            record,
            status=AttemptStatus.ENDED,
            ended_at=ended_at or record.ended_at,
            failure_reference=failure_reference or record.failure_reference,
        ))
    return tuple(reconciled)


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
    so a daemon restart cannot renew the total account. Historical frozen work is re-identified without changing saved records.
    A started legacy job with matching frozen requirements contributes at least
    one identifiable process; an unstarted pending job contributes nothing.
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
        # Re-identify historical frozen inputs with the current revision
        # algorithm without rewriting their records or renewing their budget.
        from .execution_contracts import load_job_execution_contract, source_content_identities
        frozen = load_job_execution_contract(job)
        same_frozen_work = bool(frozen.accepted and frozen.contract is not None and
            task_semantic_revision(frozen.contract.source_task,
                source_identities=source_content_identities(frozen.contract)) == task_revision)
        from .planning_inputs import load_planning_inputs
        planning = load_planning_inputs(job)
        if planning is not None:
            same_frozen_work = task_semantic_revision(
                planning.source_task, source_identities=planning.source_identities,
            ) == task_revision
        if records:
            total += sum(1 for record in records if record.task_revision == task_revision or same_frozen_work)
        elif same_frozen_work and str(getattr(job.status, "value", job.status)) != "pending":
            total += max(1, job.attempts)
    return total


def _semantic_body_sections(body: str) -> dict[str, str]:
    """Extract the semantic sections (Why/What/How/Acceptance) from a task body."""
    from open_tulid.vault.task_schema import parse_task_body
    parsed = parse_task_body(body)
    sections: dict[str, str] = {"description": _clean_section(list(parsed.description_lines))}
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


def _normalized_source_identities(
    source_identities: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Deduplicate and canonically order ``(ref, sha256)`` source identities.

    Content stored once while referenced by several links is folded to a single
    identity, and ordering is deterministic so a relisting of the same required
    sources never changes the revision.
    """
    unique: dict[str, set[str]] = {}
    for identity in source_identities:
        if not _is_string_pair(identity):
            continue
        ref, sha256 = identity
        ref = str(ref).strip()
        sha256 = str(sha256).strip()
        if not ref or not sha256:
            continue
        unique.setdefault(sha256, set()).add(ref)
    ordered: list[tuple[str, str]] = []
    for sha256 in sorted(unique):
        for ref in sorted(unique[sha256]):
            ordered.append((ref, sha256))
    return tuple(ordered)


def _is_string_pair(value: object) -> bool:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return False
    return all(isinstance(part, str) for part in value)


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
