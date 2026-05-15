from __future__ import annotations

import json
from pathlib import Path

from open_tulid.domain import (
    DomainError,
    EventActor,
    EventType,
    JournalStatus,
    TransactionJournalRecord,
)
from open_tulid.runtime import (
    JsonlEventStore,
    TransactionJournalStore,
    build_event,
    event_from_dict,
    event_to_dict,
    journal_from_dict,
    journal_to_dict,
    new_ulid,
)


class TestEventEnvelope:
    def test_new_ulid_is_crockford_26_chars(self):
        event_id = new_ulid()

        assert len(event_id) == 26
        assert set(event_id) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

    def test_event_round_trip_preserves_required_envelope(self):
        event = build_event(
            event_id="01J00000000000000000000EVT",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TaskMoved,
            correlation_id="01J00000000000000000000COR",
            task_id="01J00000000000000000000001",
            transition_id="StartWork",
            data={"from_state": "Todo", "to_state": "InProgress", "board": "Work"},
        )

        payload = event_to_dict(event)
        restored = event_from_dict(payload)

        assert payload == {
            "event_id": "01J00000000000000000000EVT",
            "schema_version": 1,
            "timestamp": "2026-05-09T12:00:00Z",
            "project_id": "Agent",
            "actor": {"type": "cli", "id": "local-user"},
            "event_type": "TaskMoved",
            "correlation_id": "01J00000000000000000000COR",
            "task_id": "01J00000000000000000000001",
            "transition_id": "StartWork",
            "data": {"from_state": "Todo", "to_state": "InProgress", "board": "Work"},
        }
        assert restored.event_id == event.event_id
        assert restored.data["board"] == "Work"


class TestJsonlEventStore:
    def test_append_writes_event_to_day_file(self, tmp_path: Path):
        store = JsonlEventStore(tmp_path / "events")
        event = build_event(
            event_id="01J00000000000000000000EVT",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TaskMoved,
            correlation_id="01J00000000000000000000COR",
        )

        result = store.append(event)

        assert result.accepted is True
        path = tmp_path / "events" / "2026-05-09.jsonl"
        assert result.path == path
        assert json.loads(path.read_text(encoding="utf-8"))["event_type"] == "TaskMoved"
        assert (tmp_path / "events" / "2026-05-09.log").read_text(encoding="utf-8") == (
            ">>> 2026-05-09T12:00:00Z TASK_MOVED\n"
        )

    def test_iter_events_reads_all_jsonl_files_in_order(self, tmp_path: Path):
        store = JsonlEventStore(tmp_path / "events")
        first = build_event(
            event_id="01J00000000000000000000E01",
            timestamp="2026-05-08T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TransitionRequested,
            correlation_id="01J00000000000000000000COR",
        )
        second = build_event(
            event_id="01J00000000000000000000E02",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TransitionAccepted,
            correlation_id="01J00000000000000000000COR",
        )

        assert store.append_many((second, first)).accepted is True

        assert [event.event_id for event in store.iter_events()] == [
            "01J00000000000000000000E01",
            "01J00000000000000000000E02",
        ]
        assert (tmp_path / "events" / "2026-05-08.log").read_text(encoding="utf-8").startswith(
            ">>> 2026-05-08T12:00:00Z TRANSITION_REQUESTED"
        )

    def test_append_returns_structured_error_for_non_json_payload(self, tmp_path: Path):
        store = JsonlEventStore(tmp_path / "events")
        event = build_event(
            event_id="01J00000000000000000000EVT",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TaskMoved,
            correlation_id="01J00000000000000000000COR",
            data={"bad": object()},
        )

        result = store.append(event)

        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "event.append_failed"

    def test_append_many_prepares_batch_before_writing_any_event(self, tmp_path: Path):
        store = JsonlEventStore(tmp_path / "events")
        good = build_event(
            event_id="01J00000000000000000000E01",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TaskMoved,
            correlation_id="01J00000000000000000000COR",
        )
        bad = build_event(
            event_id="01J00000000000000000000E02",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TaskMoved,
            correlation_id="01J00000000000000000000COR",
            data={"bad": object()},
        )

        result = store.append_many((good, bad))

        assert result.accepted is False
        assert not (tmp_path / "events" / "2026-05-09.jsonl").exists()

    def test_iter_event_records_returns_errors_for_corrupt_lines(self, tmp_path: Path):
        path = tmp_path / "events" / "2026-05-09.jsonl"
        path.parent.mkdir()
        path.write_text(
            "not json\n"
            '{"event_id":"01J00000000000000000000EVT","schema_version":1,'
            '"timestamp":"2026-05-09T12:00:00Z","project_id":"Agent",'
            '"actor":{"type":"cli","id":"local-user"},"event_type":"TaskMoved",'
            '"correlation_id":"01J00000000000000000000COR","data":{}}\n',
            encoding="utf-8",
        )
        store = JsonlEventStore(tmp_path / "events")

        records = store.iter_event_records()

        assert records[0].error is not None
        assert records[0].error.code == "event.read_failed"
        assert records[1].event is not None
        assert [event.event_id for event in store.iter_events()] == ["01J00000000000000000000EVT"]

    def test_append_rejects_timestamp_that_cannot_become_safe_day_file(self, tmp_path: Path):
        store = JsonlEventStore(tmp_path / "events")
        event = build_event(
            event_id="01J00000000000000000000EVT",
            timestamp="../../escape",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TaskMoved,
            correlation_id="01J00000000000000000000COR",
        )

        result = store.append(event)

        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "event.append_failed"


class TestTransactionJournalStore:
    def test_prepare_writes_prepared_journal(self, tmp_path: Path):
        event = build_event(
            event_id="01J00000000000000000000EVT",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TransitionAccepted,
            correlation_id="01J00000000000000000000COR",
        )
        store = TransactionJournalStore(tmp_path / "events" / "journals")

        result = store.prepare(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            task_id="01J00000000000000000000001",
            transition_id="StartWork",
            effects=({"type": "MoveKanbanCard", "task_id": "01J00000000000000000000001"},),
            events=(event,),
        )

        assert result.accepted is True
        assert result.record is not None
        assert result.record.status == JournalStatus.PREPARED
        assert result.path == tmp_path / "events" / "journals" / "01J00000000000000000000JRN.json"
        loaded = store.load("01J00000000000000000000JRN")
        assert loaded.effects[0]["type"] == "MoveKanbanCard"
        assert loaded.events[0].event_type == "TransitionAccepted"

    def test_prepare_returns_structured_error_for_non_json_effect(self, tmp_path: Path):
        store = TransactionJournalStore(tmp_path / "events" / "journals")

        result = store.prepare(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            effects=({"bad": object()},),
            events=(),
        )

        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "journal.write_failed"

    def test_commit_marks_journal_committed_and_removes_from_incomplete(self, tmp_path: Path):
        store = TransactionJournalStore(tmp_path / "events" / "journals")
        prepared = store.prepare(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            effects=(),
            events=(),
        )
        assert prepared.record is not None
        assert len(store.list_incomplete()) == 1

        committed = store.commit(prepared.record)

        assert committed.accepted is True
        assert store.load("01J00000000000000000000JRN").status == JournalStatus.COMMITTED
        assert store.list_incomplete() == ()

    def test_fail_marks_journal_failed_with_error(self, tmp_path: Path):
        store = TransactionJournalStore(tmp_path / "events" / "journals")
        prepared = store.prepare(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            effects=(),
            events=(),
        )
        assert prepared.record is not None

        failed = store.fail(prepared.record, DomainError(
            code="effect.failed",
            message="move failed",
            location="kanban/Work.md",
        ))

        assert failed.accepted is True
        loaded = store.load("01J00000000000000000000JRN")
        assert loaded.status == JournalStatus.FAILED
        assert loaded.error is not None
        assert loaded.error.code == "effect.failed"

    def test_iter_journals_can_filter_by_status(self, tmp_path: Path):
        store = TransactionJournalStore(tmp_path / "events" / "journals")
        first = store.prepare(
            journal_id="01J00000000000000000000JR1",
            project_id="Agent",
            effects=(),
            events=(),
        )
        second = store.prepare(
            journal_id="01J00000000000000000000JR2",
            project_id="Agent",
            effects=(),
            events=(),
        )
        assert first.record is not None
        assert second.record is not None
        store.commit(second.record)

        assert [record.journal_id for record in store.iter_journals(JournalStatus.PREPARED)] == [
            "01J00000000000000000000JR1",
        ]
        assert len(store.iter_journals()) == 2

    def test_journal_store_rejects_unsafe_journal_ids(self, tmp_path: Path):
        store = TransactionJournalStore(tmp_path / "events" / "journals")

        result = store.prepare(
            journal_id="../escape",
            project_id="Agent",
            effects=(),
            events=(),
        )

        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "journal.write_failed"

    def test_journal_round_trip(self):
        event = build_event(
            event_id="01J00000000000000000000EVT",
            timestamp="2026-05-09T12:00:00Z",
            project_id="Agent",
            actor=EventActor(type="cli", id="local-user"),
            event_type=EventType.TaskMoved,
            correlation_id="01J00000000000000000000COR",
        )
        record = TransactionJournalRecord(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            task_id="01J00000000000000000000001",
            transition_id="StartWork",
            started_at="2026-05-09T12:00:00Z",
            effects=({"type": "MoveKanbanCard"},),
            events=(event,),
            status=JournalStatus.PREPARED,
        )

        restored = journal_from_dict(journal_to_dict(record))

        assert restored.journal_id == record.journal_id
        assert restored.status == JournalStatus.PREPARED
        assert restored.events[0].event_id == event.event_id

    def test_journal_from_dict_rejects_malformed_effect_items(self):
        payload = {
            "journal_id": "01J00000000000000000000JRN",
            "project_id": "Agent",
            "started_at": "2026-05-09T12:00:00Z",
            "effects": ["bad"],
            "events": [],
            "status": "prepared",
        }

        try:
            journal_from_dict(payload)
        except ValueError as exc:
            assert "effects[0]" in str(exc)
        else:
            raise AssertionError("journal_from_dict accepted malformed effect")
