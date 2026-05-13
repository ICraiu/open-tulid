from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.domain import (
    DomainError,
    EventActor,
    EventEnvelope,
    EventType,
    JournalStatus,
    TransactionJournalRecord,
)


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True)
class EventAppendResult:
    path: Path | None = None
    error: DomainError | None = None

    @property
    def accepted(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class EventReadRecord:
    path: Path
    line: int
    event: EventEnvelope | None = None
    error: DomainError | None = None

    @property
    def accepted(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class JournalWriteResult:
    path: Path | None = None
    record: TransactionJournalRecord | None = None
    error: DomainError | None = None

    @property
    def accepted(self) -> bool:
        return self.error is None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_ulid() -> str:
    millis = int(time.time() * 1000)
    random_value = secrets.randbits(80)
    value = (millis << 80) | random_value
    chars = []
    for shift in range(125, -1, -5):
        chars.append(CROCKFORD[(value >> shift) & 0b11111])
    return "".join(chars)


def build_event(
    *,
    project_id: str,
    actor: EventActor,
    event_type: EventType | str,
    correlation_id: str,
    event_id: str | None = None,
    timestamp: str | None = None,
    task_id: str | None = None,
    job_id: str | None = None,
    transition_id: str | None = None,
    submission_id: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or new_ulid(),
        schema_version=1,
        timestamp=timestamp or utc_now(),
        project_id=project_id,
        actor=actor,
        event_type=event_type,
        correlation_id=correlation_id,
        task_id=task_id,
        job_id=job_id,
        transition_id=transition_id,
        submission_id=submission_id,
        data=MappingProxyType(dict(data or {})),
    )


def event_to_dict(event: EventEnvelope) -> dict[str, Any]:
    payload = {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "timestamp": event.timestamp,
        "project_id": event.project_id,
        "actor": {"type": event.actor.type, "id": event.actor.id},
        "event_type": event.event_type.value if isinstance(event.event_type, EventType) else event.event_type,
        "correlation_id": event.correlation_id,
        "data": dict(event.data),
    }
    for key in ("task_id", "job_id", "transition_id", "submission_id"):
        value = getattr(event, key)
        if value is not None:
            payload[key] = value
    return payload


def event_from_dict(payload: Mapping[str, Any]) -> EventEnvelope:
    actor_raw = payload.get("actor")
    if not isinstance(actor_raw, Mapping):
        raise ValueError("event actor must be an object")
    data_raw = payload.get("data", {})
    if not isinstance(data_raw, Mapping):
        raise ValueError("event data must be an object")
    return EventEnvelope(
        event_id=_required_string(payload, "event_id"),
        schema_version=int(payload.get("schema_version", 1)),
        timestamp=_required_string(payload, "timestamp"),
        project_id=_required_string(payload, "project_id"),
        actor=EventActor(
            type=_required_string(actor_raw, "type"),
            id=_required_string(actor_raw, "id"),
        ),
        event_type=_required_string(payload, "event_type"),
        correlation_id=_required_string(payload, "correlation_id"),
        task_id=_optional_string(payload.get("task_id")),
        job_id=_optional_string(payload.get("job_id")),
        transition_id=_optional_string(payload.get("transition_id")),
        submission_id=_optional_string(payload.get("submission_id")),
        data=MappingProxyType(dict(data_raw)),
    )


class JsonlEventStore:
    def __init__(self, root: Path):
        self.root = root

    def append(self, event: EventEnvelope) -> EventAppendResult:
        try:
            path = self._path_for(event)
            line = json.dumps(event_to_dict(event), sort_keys=True, separators=(",", ":")) + "\n"
            self._append_lines(path, (line,))
        except (OSError, TypeError, ValueError) as exc:
            return EventAppendResult(error=DomainError(
                code="event.append_failed",
                message=f"Cannot append event: {exc}",
                location=str(self.root),
            ))
        return EventAppendResult(path=path)

    def append_many(self, events: tuple[EventEnvelope, ...]) -> EventAppendResult:
        prepared: dict[Path, list[str]] = {}
        try:
            for event in events:
                path = self._path_for(event)
                line = json.dumps(event_to_dict(event), sort_keys=True, separators=(",", ":")) + "\n"
                prepared.setdefault(path, []).append(line)
        except (TypeError, ValueError) as exc:
            return EventAppendResult(error=DomainError(
                code="event.append_failed",
                message=f"Cannot append event batch: {exc}",
                location=str(self.root),
            ))

        last_path: Path | None = None
        for path, lines in prepared.items():
            try:
                self._append_lines(path, tuple(lines))
            except OSError as exc:
                return EventAppendResult(error=DomainError(
                    code="event.append_failed",
                    message=f"Cannot append event batch: {exc}",
                    location=str(path),
                ))
            last_path = path
        return EventAppendResult(path=last_path)

    def iter_events(self) -> tuple[EventEnvelope, ...]:
        return tuple(record.event for record in self.iter_event_records() if record.event is not None)

    def iter_event_records(self) -> tuple[EventReadRecord, ...]:
        records: list[EventReadRecord] = []
        if not self.root.exists():
            return ()
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    for index, line in enumerate(f, start=1):
                        if not line.strip():
                            continue
                        try:
                            payload = json.loads(line)
                            if not isinstance(payload, Mapping):
                                raise ValueError("event line must be an object")
                            records.append(EventReadRecord(path=path, line=index, event=event_from_dict(payload)))
                        except (json.JSONDecodeError, TypeError, ValueError) as exc:
                            records.append(EventReadRecord(
                                path=path,
                                line=index,
                                error=DomainError(
                                    code="event.read_failed",
                                    message=f"Cannot read event: {exc}",
                                    location=f"{path}:{index}",
                                ),
                            ))
            except OSError as exc:
                records.append(EventReadRecord(
                    path=path,
                    line=0,
                    error=DomainError(
                        code="event.read_failed",
                        message=f"Cannot open event file: {exc}",
                        location=str(path),
                    ),
                ))
        return tuple(records)

    def _append_lines(self, path: Path, lines: tuple[str, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        _fsync_directory(path.parent)

    def _path_for(self, event: EventEnvelope) -> Path:
        event_date = event.timestamp[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_date):
            raise ValueError("event timestamp must start with YYYY-MM-DD")
        return self.root / f"{event_date}.jsonl"


class TransactionJournalStore:
    def __init__(self, root: Path):
        self.root = root

    def prepare(
        self,
        *,
        journal_id: str | None,
        project_id: str,
        effects: tuple[Mapping[str, Any], ...],
        events: tuple[EventEnvelope, ...],
        task_id: str | None = None,
        transition_id: str | None = None,
    ) -> JournalWriteResult:
        record = TransactionJournalRecord(
            journal_id=journal_id or new_ulid(),
            project_id=project_id,
            task_id=task_id,
            transition_id=transition_id,
            started_at=utc_now(),
            effects=tuple(MappingProxyType(dict(effect)) for effect in effects),
            events=events,
            status=JournalStatus.PREPARED,
        )
        return self.write(record)

    def commit(self, record: TransactionJournalRecord) -> JournalWriteResult:
        return self.write(_replace_record(
            record,
            status=JournalStatus.COMMITTED,
            completed_at=utc_now(),
            error=None,
        ))

    def fail(self, record: TransactionJournalRecord, error: DomainError) -> JournalWriteResult:
        return self.write(_replace_record(
            record,
            status=JournalStatus.FAILED,
            completed_at=utc_now(),
            error=error,
        ))

    def write(self, record: TransactionJournalRecord) -> JournalWriteResult:
        try:
            path = self._path_for(record.journal_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, journal_to_dict(record))
        except (OSError, TypeError, ValueError) as exc:
            return JournalWriteResult(error=DomainError(
                code="journal.write_failed",
                message=f"Cannot write transaction journal: {exc}",
                location=str(self.root),
            ))
        return JournalWriteResult(path=path, record=record)

    def load(self, journal_id: str) -> TransactionJournalRecord:
        path = self._path_for(journal_id)
        with path.open("r", encoding="utf-8") as f:
            return journal_from_dict(json.load(f))

    def iter_journals(self, status: JournalStatus | str | None = None) -> tuple[TransactionJournalRecord, ...]:
        if not self.root.exists():
            return ()
        expected = (
            status
            if isinstance(status, JournalStatus)
            else JournalStatus(str(status)) if status is not None else None
        )
        records: list[TransactionJournalRecord] = []
        for path in sorted(self.root.glob("*.json")):
            with path.open("r", encoding="utf-8") as f:
                record = journal_from_dict(json.load(f))
            if expected is None or record.status == expected:
                records.append(record)
        return tuple(records)

    def list_incomplete(self) -> tuple[TransactionJournalRecord, ...]:
        return self.iter_journals(JournalStatus.PREPARED)

    def _path_for(self, journal_id: str) -> Path:
        return self.root / f"{_safe_stem(journal_id)}.json"


def journal_to_dict(record: TransactionJournalRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "journal_id": record.journal_id,
        "project_id": record.project_id,
        "started_at": record.started_at,
        "effects": [dict(effect) for effect in record.effects],
        "events": [event_to_dict(event) for event in record.events],
        "status": record.status.value if isinstance(record.status, JournalStatus) else record.status,
    }
    for key in ("task_id", "transition_id", "completed_at"):
        value = getattr(record, key)
        if value is not None:
            payload[key] = value
    if record.error is not None:
        payload["error"] = asdict(record.error)
    return payload


def journal_from_dict(payload: Mapping[str, Any]) -> TransactionJournalRecord:
    effects_raw = payload.get("effects", [])
    events_raw = payload.get("events", [])
    if not isinstance(effects_raw, list):
        raise ValueError("journal effects must be a list")
    if not isinstance(events_raw, list):
        raise ValueError("journal events must be a list")
    effects = []
    for index, effect in enumerate(effects_raw):
        if not isinstance(effect, Mapping):
            raise ValueError(f"journal effects[{index}] must be an object")
        effects.append(MappingProxyType(dict(effect)))
    events = []
    for index, event in enumerate(events_raw):
        if not isinstance(event, Mapping):
            raise ValueError(f"journal events[{index}] must be an object")
        events.append(event_from_dict(event))
    status = JournalStatus(str(payload.get("status")))
    error_raw = payload.get("error")
    error = None
    if isinstance(error_raw, Mapping):
        error = DomainError(
            code=_required_string(error_raw, "code"),
            message=_required_string(error_raw, "message"),
            location=_optional_string(error_raw.get("location")),
        )
    return TransactionJournalRecord(
        journal_id=_required_string(payload, "journal_id"),
        project_id=_required_string(payload, "project_id"),
        task_id=_optional_string(payload.get("task_id")),
        transition_id=_optional_string(payload.get("transition_id")),
        started_at=_required_string(payload, "started_at"),
        effects=tuple(effects),
        events=tuple(events),
        status=status,
        completed_at=_optional_string(payload.get("completed_at")),
        error=error,
    )


def _replace_record(
    record: TransactionJournalRecord,
    *,
    status: JournalStatus,
    completed_at: str,
    error: DomainError | None,
) -> TransactionJournalRecord:
    return TransactionJournalRecord(
        journal_id=record.journal_id,
        project_id=record.project_id,
        task_id=record.task_id,
        transition_id=record.transition_id,
        started_at=record.started_at,
        effects=record.effects,
        events=record.events,
        status=status,
        completed_at=completed_at,
        error=error,
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        finally:
            raise


def _safe_stem(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("journal id must be a safe file stem")
    if Path(value).name != value:
        raise ValueError("journal id must be a safe file stem")
    return value


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
