from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from open_tulid.cli.main import app
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME


runner = CliRunner()


def _write_config(root: Path) -> None:
    config_dir = root / CONFIG_DIRNAME
    config_dir.mkdir()
    (config_dir / CONFIG_FILENAME).write_text(
        f'[vault]\nroot = "{root}"\nprojects = ["Agent"]\n',
        encoding="utf-8",
    )
    (root / "Agent").mkdir()


def _with_cwd(path: Path):
    class Cwd:
        def __enter__(self):
            self.original = os.getcwd()
            os.chdir(path)

        def __exit__(self, exc_type, exc, tb):
            os.chdir(self.original)

    return Cwd()


def test_log_with_count_prints_recent_human_event_lines(tmp_path: Path):
    _write_config(tmp_path)
    events = tmp_path / "Agent" / "events"
    events.mkdir()
    (events / "2026-05-14.log").write_text(
        ">>> 2026-05-14T10:00:00Z FIRST\n"
        ">>> 2026-05-14T10:01:00Z SECOND\n",
        encoding="utf-8",
    )
    (events / "2026-05-15.log").write_text(
        ">>> 2026-05-15T10:00:00Z THIRD\n",
        encoding="utf-8",
    )

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["log", "2"])

    assert result.exit_code == 0
    assert result.output == (
        ">>> 2026-05-14T10:01:00Z SECOND\n"
        ">>> 2026-05-15T10:00:00Z THIRD\n"
    )


def test_scheduler_start_writes_background_process_state(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)

    class FakeProcess:
        pid = 4321

    seen: dict[str, object] = {}

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("open_tulid.cli.main.subprocess.Popen", fake_popen)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["scheduler", "start", "--interval", "5"])

    state_path = tmp_path / "Agent" / ".open-tulid" / "scheduler.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.exit_code == 0
    assert "Scheduler started for Agent pid=4321" in result.output
    assert seen["args"][-5:] == ["jobs", "daemon", "Agent", "--interval", "5.0"]
    assert state["pid"] == 4321
    assert state["project"] == "Agent"
    assert state["interval"] == 5.0


def test_scheduler_status_reports_running_process(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / "Agent" / ".open-tulid" / "scheduler.json"
    state_path.parent.mkdir()
    state_path.write_text('{"pid": 4321, "project": "Agent"}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value == 4321)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["scheduler", "status"])

    assert result.exit_code == 0
    assert result.output == "Scheduler running for Agent pid=4321\n"


def test_scheduler_stop_signals_process_and_removes_state(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / "Agent" / ".open-tulid" / "scheduler.json"
    state_path.parent.mkdir()
    state_path.write_text('{"pid": 4321, "project": "Agent"}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value == 4321)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: killed.append((pid, sig)))

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["scheduler", "stop"])

    assert result.exit_code == 0
    assert result.output == "Scheduler stopped for Agent\n"
    assert killed == [(4321, 15)]
    assert not state_path.exists()
