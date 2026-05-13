from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from open_tulid.cli.main import app
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME
from open_tulid.domain import DomainError, EventActor, EventType
from open_tulid.runtime import JsonlEventStore, TransactionJournalStore, build_event


runner = CliRunner()


def _write_config(root: Path) -> None:
    config_dir = root / CONFIG_DIRNAME
    config_dir.mkdir()
    (config_dir / CONFIG_FILENAME).write_text(
        f'[vault]\nroot = "{root}"\nprojects = ["Agent"]\n',
        encoding="utf-8",
    )


def _with_cwd(path: Path):
    class Cwd:
        def __enter__(self):
            self.original = os.getcwd()
            os.chdir(path)

        def __exit__(self, exc_type, exc, tb):
            os.chdir(self.original)

    return Cwd()


def test_events_list_shows_recent_project_events(tmp_path: Path):
    _write_config(tmp_path)
    project = tmp_path / "Agent"
    project.mkdir()
    event = build_event(
        event_id="01J00000000000000000000EVT",
        timestamp="2026-05-09T12:00:00Z",
        project_id="Agent",
        actor=EventActor(type="cli", id="local-user"),
        event_type=EventType.OperationFinished,
        correlation_id="01J00000000000000000000COR",
        task_id="TASK-1",
        transition_id="StartWork",
        data={"operation": "move_task"},
    )
    assert JsonlEventStore(project / "events").append(event).accepted is True

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["events", "list", "Agent"])

    assert result.exit_code == 0
    assert "OperationFinished" in result.output
    assert "task=TASK-1" in result.output
    assert "transition=StartWork" in result.output


def test_events_list_reports_corrupt_records_without_hiding_valid_events(tmp_path: Path):
    _write_config(tmp_path)
    project = tmp_path / "Agent"
    events_dir = project / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "2026-05-09.jsonl").write_text(
        '{"not":"an event"}\n'
        '{"event_id":"01J00000000000000000000EVT","schema_version":1,'
        '"timestamp":"2026-05-09T12:00:00Z","project_id":"Agent",'
        '"actor":{"type":"cli","id":"local-user"},"event_type":"OperationStarted",'
        '"correlation_id":"01J00000000000000000000COR","data":{}}\n',
        encoding="utf-8",
    )

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["events", "list", "Agent"])

    assert result.exit_code == 0
    assert "Skipped corrupt event" in result.output
    assert "OperationStarted" in result.output


def test_events_status_shows_prepared_and_failed_journals(tmp_path: Path):
    _write_config(tmp_path)
    project = tmp_path / "Agent"
    project.mkdir()
    store = TransactionJournalStore(project / "events" / "journals")
    prepared = store.prepare(
        journal_id="01J00000000000000000000JR1",
        project_id="Agent",
        task_id="TASK-1",
        effects=(),
        events=(),
    )
    assert prepared.record is not None
    failed = store.prepare(
        journal_id="01J00000000000000000000JR2",
        project_id="Agent",
        effects=(),
        events=(),
    )
    assert failed.record is not None
    assert store.fail(failed.record, DomainError(code="effect.failed", message="failed")).accepted is True

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["events", "status", "Agent"])

    assert result.exit_code == 0
    assert "prepared=1 committed=0 failed=1" in result.output
    assert "prepared 01J00000000000000000000JR1 task=TASK-1" in result.output
    assert "failed 01J00000000000000000000JR2" in result.output
