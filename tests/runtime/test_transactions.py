from __future__ import annotations

from pathlib import Path

from open_tulid.domain import DomainError, EventActor, EventType, JournalStatus
from open_tulid.runtime import (
    FileTransactionRuntime,
    JsonlEventStore,
    TransactionJournalStore,
    build_event,
)
from open_tulid.workflow.implementations import OperationResult


def _event():
    return build_event(
        event_id="01J00000000000000000000EVT",
        timestamp="2026-05-09T12:00:00Z",
        project_id="Agent",
        actor=EventActor(type="cli", id="local-user"),
        event_type=EventType.TaskMoved,
        correlation_id="01J00000000000000000000COR",
        task_id="01J00000000000000000000001",
        transition_id="StartWork",
    )


def _runtime(tmp_path: Path, apply_effect):
    return FileTransactionRuntime(
        journals=TransactionJournalStore(tmp_path / "events" / "journals"),
        events=JsonlEventStore(tmp_path / "events"),
        apply_effect=apply_effect,
    )


class TestFileTransactionRuntime:
    def test_prepare_applies_effects_appends_events_then_commits(self, tmp_path: Path):
        calls: list[dict[str, object]] = []

        def apply_effect(effect):
            calls.append(dict(effect))
            return OperationResult(accepted=True, code="ok")

        runtime = _runtime(tmp_path, apply_effect)

        result = runtime.apply(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            task_id="01J00000000000000000000001",
            transition_id="StartWork",
            effects=({"type": "MoveKanbanCard"},),
            events=(_event(),),
        )

        assert result.accepted is True
        assert calls == [{"type": "MoveKanbanCard"}]
        assert result.journal is not None
        assert result.journal.status == JournalStatus.COMMITTED
        assert (tmp_path / "events" / "2026-05-09.jsonl").is_file()

    def test_effect_failure_marks_journal_failed_and_does_not_append_events(self, tmp_path: Path):
        def apply_effect(effect):
            return OperationResult(accepted=False, code="move_task", message="move failed")

        runtime = _runtime(tmp_path, apply_effect)

        result = runtime.apply(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            effects=({"type": "MoveKanbanCard"},),
            events=(_event(),),
        )

        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "effect.failed"
        journal = TransactionJournalStore(tmp_path / "events" / "journals").load("01J00000000000000000000JRN")
        assert journal.status == JournalStatus.FAILED
        assert not (tmp_path / "events" / "2026-05-09.jsonl").exists()

    def test_effect_exception_marks_journal_failed_and_does_not_append_events(self, tmp_path: Path):
        def apply_effect(effect):
            raise RuntimeError("boom")

        runtime = _runtime(tmp_path, apply_effect)

        result = runtime.apply(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            effects=({"type": "MoveKanbanCard"},),
            events=(_event(),),
        )

        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "effect.exception"
        journal = TransactionJournalStore(tmp_path / "events" / "journals").load("01J00000000000000000000JRN")
        assert journal.status == JournalStatus.FAILED
        assert not (tmp_path / "events" / "2026-05-09.jsonl").exists()

    def test_event_append_failure_marks_journal_failed_after_effects(self, tmp_path: Path):
        calls: list[dict[str, object]] = []

        def apply_effect(effect):
            calls.append(dict(effect))
            return OperationResult(accepted=True, code="ok")

        runtime = FileTransactionRuntime(
            journals=TransactionJournalStore(tmp_path / "events" / "journals"),
            events=JsonlEventStore(tmp_path / "events-as-file"),
            apply_effect=apply_effect,
        )
        (tmp_path / "events-as-file").write_text("not a dir", encoding="utf-8")

        result = runtime.apply(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            effects=({"type": "MoveKanbanCard"},),
            events=(_event(),),
        )

        assert calls == [{"type": "MoveKanbanCard"}]
        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "event.append_failed"
        journal = TransactionJournalStore(tmp_path / "events" / "journals").load("01J00000000000000000000JRN")
        assert journal.status == JournalStatus.FAILED

    def test_journal_prepare_failure_prevents_effects(self, tmp_path: Path):
        calls: list[dict[str, object]] = []

        def apply_effect(effect):
            calls.append(dict(effect))
            return OperationResult(accepted=True, code="ok")

        journal_root = tmp_path / "journals-as-file"
        journal_root.write_text("not a dir", encoding="utf-8")
        runtime = FileTransactionRuntime(
            journals=TransactionJournalStore(journal_root),
            events=JsonlEventStore(tmp_path / "events"),
            apply_effect=apply_effect,
        )

        result = runtime.apply(
            journal_id="01J00000000000000000000JRN",
            project_id="Agent",
            effects=({"type": "MoveKanbanCard"},),
            events=(_event(),),
        )

        assert result.accepted is False
        assert result.error is not None
        assert result.error.code == "journal.write_failed"
        assert calls == []
