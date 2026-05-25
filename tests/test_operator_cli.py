from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from open_tulid.adapters import default_adapter_type
from open_tulid.cli import main as cli_main
from open_tulid.cli.main import app
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME
from open_tulid.runtime.scheduler import ScheduleResult
from open_tulid.domain import ExecutionJob
from open_tulid.runtime import FileExecutionJobStore


runner = CliRunner()


def _write_config(root: Path, projects: tuple[str, ...] = ("Agent",)) -> None:
    config_dir = root / CONFIG_DIRNAME
    config_dir.mkdir()
    tracker_type = default_adapter_type()
    projects_yaml = "\n".join(f"  {project}: {{}}" for project in projects)
    (config_dir / CONFIG_FILENAME).write_text(
        f"tracker:\n  type: {tracker_type}\n  root: {root}\nprojects:\n{projects_yaml}\n",
        encoding="utf-8",
    )
    for project in projects:
        (root / project).mkdir()


def _with_cwd(path: Path):
    class Cwd:
        def __enter__(self):
            self.original = os.getcwd()
            self.original_home = os.environ.get("HOME")
            os.environ["HOME"] = str(path)
            os.chdir(path)

        def __exit__(self, exc_type, exc, tb):
            os.chdir(self.original)
            if self.original_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = self.original_home

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


def test_runtime_start_writes_background_process_state(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)

    processes = iter((type("ProxyProcess", (), {"pid": 4321})(), type("SchedulerProcess", (), {"pid": 9876})()))

    seen: dict[str, object] = {}

    def fake_popen(args, **kwargs):
        seen.setdefault("calls", []).append((args, kwargs))
        return next(processes)

    monkeypatch.setattr("open_tulid.cli.main.subprocess.Popen", fake_popen)
    monkeypatch.setattr("open_tulid.cli.main.check_backend_readiness", lambda *args, **kwargs: ())
    monkeypatch.setattr("open_tulid.cli.main._proxy_listener_ready", lambda config: True)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "start", "--interval", "5"])

    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.exit_code == 0
    assert "MODEL_PROXY_STARTED pid=4321" in result.output
    assert "SCHEDULER_STARTED project=Agent pid=9876 interval=5.0" in result.output
    assert seen["calls"][0][0][-4:] == [sys.executable, "-m", "open_tulid", "model-proxy", "serve"][-4:]
    assert seen["calls"][1][0][-7:] == [sys.executable, "-m", "open_tulid", "jobs", "daemon", "Agent", "--interval", "5.0"][-7:]
    assert state["scheduler_pid"] == 9876
    proxy_state = json.loads((tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json").read_text(encoding="utf-8"))
    assert proxy_state["proxy_pid"] == 4321
    assert state["project"] == "Agent"
    assert state["interval"] == 5.0


def test_runtime_start_without_project_starts_all_configured_projects(tmp_path: Path, monkeypatch):
    _write_config(tmp_path, projects=("Agent", "Beta"))

    processes = iter((
        type("ProxyProcess", (), {"pid": 4321})(),
        type("SchedulerProcess", (), {"pid": 9876})(),
        type("SchedulerProcess", (), {"pid": 9877})(),
    ))
    seen: list[tuple[object, object]] = []

    def fake_popen(args, **kwargs):
        seen.append((args, kwargs))
        return next(processes)

    monkeypatch.setattr("open_tulid.cli.main.subprocess.Popen", fake_popen)
    monkeypatch.setattr("open_tulid.cli.main.check_backend_readiness", lambda *args, **kwargs: ())
    monkeypatch.setattr("open_tulid.cli.main._proxy_listener_ready", lambda config: True)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "start", "--interval", "5"])

    agent_state = json.loads((tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json").read_text(encoding="utf-8"))
    beta_state = json.loads((tmp_path / CONFIG_DIRNAME / "runtime" / "Beta.json").read_text(encoding="utf-8"))
    assert result.exit_code == 0
    assert "MODEL_PROXY_STARTED pid=4321" in result.output
    assert "SCHEDULER_STARTED project=Agent pid=9876 interval=5.0" in result.output
    assert "SCHEDULER_STARTED project=Beta pid=9877 interval=5.0" in result.output
    assert seen[1][0][-7:] == [sys.executable, "-m", "open_tulid", "jobs", "daemon", "Agent", "--interval", "5.0"][-7:]
    assert seen[2][0][-7:] == [sys.executable, "-m", "open_tulid", "jobs", "daemon", "Beta", "--interval", "5.0"][-7:]
    assert agent_state["scheduler_pid"] == 9876
    assert beta_state["scheduler_pid"] == 9877


def test_runtime_start_reuses_running_scheduler_when_only_proxy_is_down(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 9876, "project": "Agent", "interval": 5.0}', encoding="utf-8")
    seen = []
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value == 9876)
    monkeypatch.setattr("open_tulid.cli.main.subprocess.Popen", lambda *args, **kwargs: seen.append(args) or type("P", (), {"pid": 4321})())
    monkeypatch.setattr("open_tulid.cli.main.check_backend_readiness", lambda *args, **kwargs: ())
    monkeypatch.setattr("open_tulid.cli.main._proxy_listener_ready", lambda config: True)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "start"])

    assert result.exit_code == 0
    assert "SCHEDULER_ALREADY_RUNNING project=Agent pid=9876" in result.output
    assert len(seen) == 1


def test_runtime_start_fails_active_jobs_from_dead_prior_scheduler(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 9876, "project": "Agent", "interval": 5.0}', encoding="utf-8")
    job_store = FileExecutionJobStore(tmp_path / CONFIG_DIRNAME / "jobs" / "Agent")
    assert job_store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id="task-1",
        transition_id="code",
        worker_id="codex",
        workspace_path=str(tmp_path / "workspace"),
    )).accepted is True
    assert job_store.update_status("01J00000000000000000000JOB", "running").accepted is True

    processes = iter((type("ProxyProcess", (), {"pid": 4321})(), type("SchedulerProcess", (), {"pid": 6543})()))
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: False)
    monkeypatch.setattr("open_tulid.cli.main.subprocess.Popen", lambda *args, **kwargs: next(processes))
    monkeypatch.setattr("open_tulid.cli.main.check_backend_readiness", lambda *args, **kwargs: ())
    monkeypatch.setattr("open_tulid.cli.main._proxy_listener_ready", lambda config: True)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "start"])

    loaded = job_store.get("01J00000000000000000000JOB")
    assert result.exit_code == 0
    assert "JOB_ORPHANED project=Agent" in result.output
    assert "job=01J00000000000000000000JOB prior_status=running" in result.output
    assert loaded.job is not None
    assert loaded.job.status == "failed"


def test_proxy_listener_ready_retries_until_listener_accepts(monkeypatch):
    attempts = {"count": 0}

    class DummySocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_create_connection(address, timeout):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise OSError("not ready")
        return DummySocket()

    monkeypatch.setattr("open_tulid.cli.main.socket.create_connection", fake_create_connection)
    monkeypatch.setattr("open_tulid.cli.main.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "open_tulid.cli.main.time.monotonic",
        iter((0.0, 0.0, 0.1, 0.2)).__next__,
    )

    config = type("Cfg", (), {
        "model_proxy_server": type("ProxyCfg", (), {"host": "0.0.0.0", "port": 8787})(),
    })()

    assert cli_main._proxy_listener_ready(config, timeout=1.0, poll_interval=0.05) is True
    assert attempts["count"] == 3




def test_runtime_status_reports_running_processes(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 4321, "proxy_pid": 9876, "project": "Agent"}', encoding="utf-8")
    (tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json").write_text('{"proxy_pid": 9876}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in {4321, 9876})

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "status"])

    assert result.exit_code == 0
    assert result.output == "Runtime running for Agent scheduler_pid=4321 proxy_pid=9876\n"


def test_runtime_status_without_project_reports_all_projects(tmp_path: Path, monkeypatch):
    _write_config(tmp_path, projects=("Agent", "Beta"))
    state_root = tmp_path / CONFIG_DIRNAME / "runtime"
    state_root.mkdir(parents=True)
    (state_root / "Agent.json").write_text('{"scheduler_pid": 4321, "project": "Agent"}', encoding="utf-8")
    (state_root / "Beta.json").write_text('{"scheduler_pid": 5432, "project": "Beta"}', encoding="utf-8")
    (tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json").write_text('{"proxy_pid": 9876}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in {4321, 5432, 9876})

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "status"])

    assert result.exit_code == 0
    assert result.output == (
        "Runtime running for Agent scheduler_pid=4321 proxy_pid=9876\n"
        "\n"
        "Runtime running for Beta scheduler_pid=5432 proxy_pid=9876\n"
    )


def test_runtime_stop_signals_processes_and_removes_state(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 4321, "proxy_pid": 9876, "project": "Agent"}', encoding="utf-8")
    proxy_state = tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json"
    proxy_state.write_text('{"proxy_pid": 9876}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in {4321, 9876})
    monkeypatch.setattr("open_tulid.cli.main._wait_for_pid_exit", lambda pid: True)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: killed.append((pid, sig)))

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "stop"])

    assert result.exit_code == 0
    assert "MODEL_PROXY_STOPPED pid=9876" in result.output
    assert result.output.endswith("Runtime stopped for Agent\n")
    assert killed == [(4321, 15), (9876, 15)]
    assert not state_path.exists()
    assert not proxy_state.exists()


def test_runtime_stop_without_project_stops_all_projects(tmp_path: Path, monkeypatch):
    _write_config(tmp_path, projects=("Agent", "Beta"))
    state_root = tmp_path / CONFIG_DIRNAME / "runtime"
    state_root.mkdir(parents=True)
    (state_root / "Agent.json").write_text('{"scheduler_pid": 4321, "project": "Agent"}', encoding="utf-8")
    (state_root / "Beta.json").write_text('{"scheduler_pid": 5432, "project": "Beta"}', encoding="utf-8")
    proxy_state = tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json"
    proxy_state.write_text('{"proxy_pid": 9876}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in {4321, 5432, 9876})
    monkeypatch.setattr("open_tulid.cli.main._wait_for_pid_exit", lambda pid: True)
    monkeypatch.setattr("open_tulid.cli.main._reconcile_active_runtime_jobs", lambda *args, **kwargs: None)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: killed.append((pid, sig)))

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "stop"])

    assert result.exit_code == 0
    assert "MODEL_PROXY_STOPPED pid=9876" in result.output
    assert result.output.endswith("Runtime stopped for 2 projects\n")
    assert killed == [(4321, 15), (5432, 15), (9876, 15)]
    assert not (state_root / "Agent.json").exists()
    assert not (state_root / "Beta.json").exists()
    assert not proxy_state.exists()


def test_runtime_stop_fails_active_jobs_and_stops_worker_containers(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 4321, "project": "Agent"}', encoding="utf-8")
    proxy_state = tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json"
    proxy_state.write_text('{"proxy_pid": 9876}', encoding="utf-8")
    job_store = FileExecutionJobStore(tmp_path / CONFIG_DIRNAME / "jobs" / "Agent")
    assert job_store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id="task-1",
        transition_id="code",
        worker_id="codex",
        workspace_path=str(tmp_path / "workspace"),
    )).accepted is True
    assert job_store.update_status("01J00000000000000000000JOB", "running").accepted is True
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in {4321, 9876})
    monkeypatch.setattr("open_tulid.cli.main._wait_for_pid_exit", lambda pid: True)
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: None)
    docker_calls: list[tuple[str, ...]] = []

    def fake_run(args, *, check, capture_output, text, timeout):
        docker_calls.append(tuple(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("open_tulid.cli.main.subprocess.run", fake_run)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "stop"])

    loaded = job_store.get("01J00000000000000000000JOB")
    assert result.exit_code == 0
    assert "WORKER_CONTAINER_STOPPED job=01J00000000000000000000JOB" in result.output
    assert "name=open-tulid-job-01j00000000000000000000job" in result.output
    assert "JOB_ORPHANED project=Agent" in result.output
    assert "job=01J00000000000000000000JOB prior_status=running" in result.output
    assert loaded.job is not None
    assert loaded.job.status == "failed"
    assert docker_calls == [("docker", "rm", "-f", "open-tulid-job-01j00000000000000000000job")]


def test_runtime_stop_waits_for_scheduler_before_proxy_decision(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 4321, "project": "Agent"}', encoding="utf-8")
    proxy_state = tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json"
    proxy_state.write_text('{"proxy_pid": 9876}', encoding="utf-8")
    alive = {4321, 9876}
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in alive)

    waited = []

    def fake_wait(pid):
        waited.append(pid)
        alive.discard(pid)
        return True

    monkeypatch.setattr("open_tulid.cli.main._wait_for_pid_exit", fake_wait)
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: None)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "stop"])

    assert result.exit_code == 0
    assert waited == [4321, 9876]
    assert "MODEL_PROXY_STOPPED pid=9876" in result.output


def test_runtime_stop_refuses_to_claim_success_when_scheduler_does_not_exit(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 4321, "project": "Agent"}', encoding="utf-8")
    (tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json").write_text('{"proxy_pid": 9876}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in {4321, 9876})
    monkeypatch.setattr("open_tulid.cli.main._wait_for_pid_exit", lambda pid: False)
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: None)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "stop"])

    assert result.exit_code == 1
    assert "SCHEDULER_STOP_TIMEOUT project=Agent pid=4321" in result.output
    assert state_path.exists()


def test_runtime_stop_refuses_to_claim_success_when_proxy_does_not_exit(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    state_path = tmp_path / CONFIG_DIRNAME / "runtime" / "Agent.json"
    state_path.parent.mkdir()
    state_path.write_text('{"scheduler_pid": 4321, "project": "Agent"}', encoding="utf-8")
    proxy_state = tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json"
    proxy_state.write_text('{"proxy_pid": 9876}', encoding="utf-8")
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value in {4321, 9876})
    waits = iter((True, False))
    monkeypatch.setattr("open_tulid.cli.main._wait_for_pid_exit", lambda pid: next(waits))
    monkeypatch.setattr("open_tulid.cli.main._any_scheduler_running", lambda config: False)
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: None)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "stop"])

    assert result.exit_code == 1
    assert "MODEL_PROXY_STOP_TIMEOUT pid=9876" in result.output
    assert proxy_state.exists()


def test_runtime_start_stops_new_proxy_when_scheduler_fails(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)

    class Process:
        def __init__(self, pid, alive):
            self.pid = pid
            self.alive = alive

        def poll(self):
            return None if self.alive else 1

    processes = iter((Process(4321, True), Process(9876, False)))
    monkeypatch.setattr("open_tulid.cli.main.subprocess.Popen", lambda *args, **kwargs: next(processes))
    monkeypatch.setattr("open_tulid.cli.main.check_backend_readiness", lambda *args, **kwargs: ())
    monkeypatch.setattr("open_tulid.cli.main._proxy_listener_ready", lambda config: True)
    monkeypatch.setattr("open_tulid.cli.main._pid_is_running", lambda value: value == 4321)
    killed = []
    monkeypatch.setattr("open_tulid.cli.main.os.kill", lambda pid, sig: killed.append((pid, sig)))

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["runtime", "start"])

    assert result.exit_code == 1
    assert "SCHEDULER_START_FAILED" in result.output
    assert "MODEL_PROXY_STOPPED_AFTER_START_FAILURE pid=4321" in result.output
    assert killed == [(4321, 15)]
    assert not (tmp_path / CONFIG_DIRNAME / "model-proxy-runtime.json").exists()


def test_jobs_daemon_logs_failed_job_and_keeps_control_flow(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    fake_ctx = {
        "workflow": object(),
        "adapter": object(),
        "job_store": object(),
        "workspace_root": tmp_path / "workspaces",
        "lease_store": object(),
        "config": SimpleNamespace(
            runtime=SimpleNamespace(
                worker_resources={},
                repo_execution_mode="serial",
            ),
        ),
        "event_store": SimpleNamespace(append_many=lambda events: None),
        "journal_store": object(),
    }
    fake_job = SimpleNamespace(
        job_id="job-1",
        task_id="task-1",
        transition_id="implement",
        worker_id="codex",
    )
    monkeypatch.setattr("open_tulid.cli.main._runtime_project_context", lambda project: fake_ctx)
    monkeypatch.setattr(
        "open_tulid.cli.main.Scheduler",
        lambda **kwargs: SimpleNamespace(schedule_one=lambda project: ScheduleResult(scheduled=True, job=fake_job)),
    )
    monkeypatch.setattr("open_tulid.cli.main.run_job", lambda project, job_id: (_ for _ in ()).throw(typer.Exit(1)))

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["jobs", "daemon", "Agent", "--limit", "1"])

    assert result.exit_code == 0
    assert "JOB_RUN_FAILED project=Agent job=job-1 exit_code=1" in result.output


def test_tasks_list_without_project_lists_all_projects_grouped(tmp_path: Path, monkeypatch):
    _write_config(tmp_path, projects=("Agent", "Beta"))

    snapshots = {
        "Agent": SimpleNamespace(tasks={
            "1": SimpleNamespace(id="1", current_state="Todo", task_type="ImplementationTask", title="Ship agent"),
        }),
        "Beta": SimpleNamespace(tasks={
            "2": SimpleNamespace(id="2", current_state="Done", task_type="ProductIdea", title="Ship beta"),
        }),
    }

    def fake_context(project):
        return {
            "adapter": SimpleNamespace(load_project=lambda: SimpleNamespace(
                accepted=True,
                snapshot=snapshots[project],
                errors=(),
            )),
        }

    monkeypatch.setattr("open_tulid.cli.main._runtime_project_context", fake_context)

    with _with_cwd(tmp_path):
        result = runner.invoke(app, ["tasks", "list"])

    assert result.exit_code == 0
    assert result.output == (
        "Agent:\n"
        "  1  state=Todo  type=ImplementationTask  title=Ship agent\n"
        "\n"
        "Beta:\n"
        "  2  state=Done  type=ProductIdea  title=Ship beta\n"
    )
