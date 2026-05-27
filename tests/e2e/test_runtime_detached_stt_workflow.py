from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from open_tulid.adapters import default_adapter_type
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME
from open_tulid.runtime import JsonlEventStore


FIXTURES = Path(__file__).parent / "fixtures"
REPO_SRC = Path(__file__).resolve().parents[2] / "src"


@dataclass(frozen=True)
class RuntimeProject:
    root: Path
    vault: Path
    project: Path
    repo: Path
    runtime_logs: Path


@pytest.fixture(scope="session")
def scripted_runtime_worker_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    if not _docker_available():
        pytest.skip("Docker daemon is not available for Docker-backed E2E tests.")

    context = tmp_path_factory.mktemp("open-tulid-scripted-runtime-worker")
    tag = f"open-tulid/e2e-scripted-runtime:{_safe_tag(context.name)}"
    shutil.copytree(FIXTURES / "scripted-worker-runtime", context, dirs_exist_ok=True)

    built = subprocess.run(
        ("docker", "build", "-t", tag, str(context)),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if built.returncode != 0:
        pytest.skip(f"Cannot build scripted runtime worker image: {built.stderr.strip()}")

    yield tag

    subprocess.run(("docker", "rmi", "-f", tag), check=False, capture_output=True, text=True)


def test_runtime_start_drives_stt_style_workflow_end_to_end(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _make_runtime_project(tmp_path, scripted_runtime_worker_image)

    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr
        assert "MODEL_PROXY_STARTED" in started.stdout
        assert "SCHEDULER_STARTED project=Agent" in started.stdout

        _wait_for(
            lambda: _task_state(project.project, "1") == "HumanReview",
            "product idea to reach HumanReview",
        )
        _wait_for(
            lambda: "scheduler.no_transition" in _scheduler_stdout(project),
            "scheduler to stop at the manual HumanReview gate",
        )

        manual = _run_tulid(project.root, "transition", "Agent", "1", "ApproveDirection")
        assert manual.returncode == 0, manual.stdout + manual.stderr
        assert "Moved 1 to ReadyForSpec." in manual.stdout

        _wait_for(
            lambda: _task_state(project.project, "1") == "Done" and _task_state(project.project, "2") == "Done",
            "parent and derived implementation task to finish",
            timeout=40.0,
        )

        _wait_for(
            lambda: _board_contains(project.project, "## Done", "[[1-stt-clipboard]]")
            and _board_contains(project.project, "## Done", "[[2-implement-healthz-endpoint]]"),
            "board to show both tasks in Done",
        )

        event_store = JsonlEventStore(project.project / "events")
        events = list(event_store.iter_events())
        transition_ids = {
            event.transition_id
            for event in events
            if event.event_type == "TransitionAccepted" and event.transition_id is not None
        }
        assert {
            "DraftDirection",
            "ApproveDirection",
            "WriteImplementationSpec",
            "BreakDownImplementationSpec",
            "ImplementTask",
            "SelfReviewPass1",
            "SelfReviewPass2",
        }.issubset(transition_ids)

        scheduler_stdout = _scheduler_stdout(project)
        assert "SCHEDULER_SCHEDULED project=Agent" in scheduler_stdout
        for transition_id in (
            "DraftDirection",
            "WriteImplementationSpec",
            "BreakDownImplementationSpec",
            "ImplementTask",
            "SelfReviewPass1",
            "SelfReviewPass2",
        ):
            assert f"transition={transition_id}" in scheduler_stdout
        assert "SCHEDULER_SKIPPED project=Agent" in scheduler_stdout
        assert "code=scheduler.no_transition" in scheduler_stdout

        project_log = _run_tulid(project.root, "log", "200", "--project", "Agent")
        assert project_log.returncode == 0, project_log.stdout + project_log.stderr
        compact_log = " ".join(project_log.stdout.split())
        assert "TRANSITION_ACCEPTED task=1 job=" in compact_log
        assert "transition=DraftDirection" in compact_log
        assert "TRANSITION_ACCEPTED task=1 transition=ApproveDirection" in compact_log
        assert "TRANSITION_ACCEPTED task=2 job=" in compact_log
        assert "transition=SelfReviewPass2" in compact_log
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def _make_runtime_project(tmp_path: Path, worker_image: str) -> RuntimeProject:
    root = tmp_path / "workspace"
    vault = root / "vault"
    project = vault / "Agent"
    repo = root / "repo"
    fixture_project = FIXTURES / "runtime-stt-project"

    shutil.copytree(fixture_project / "Agent", project)
    shutil.copytree(fixture_project / "repo", repo)
    shutil.copy2(fixture_project / "workflow.yaml", project / "workflow.yaml")
    (root / CONFIG_DIRNAME).mkdir(parents=True)
    config_template = (fixture_project / "config.yaml.template").read_text(encoding="utf-8")
    (root / CONFIG_DIRNAME / CONFIG_FILENAME).write_text(
        config_template.format(
            tracker_type=default_adapter_type(),
            vault_root=vault,
            repo_root=repo,
            worker_image=worker_image,
        ),
        encoding="utf-8",
    )
    return RuntimeProject(
        root=root,
        vault=vault,
        project=project,
        repo=repo,
        runtime_logs=root / CONFIG_DIRNAME / "runtime-logs",
    )


def _run_tulid(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(REPO_SRC)
        if not existing_pythonpath
        else f"{REPO_SRC}{os.pathsep}{existing_pythonpath}"
    )
    python_executable = _project_python()
    return subprocess.run(
        (
            python_executable,
            "-m",
            "open_tulid",
            *args,
        ),
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _project_python() -> str:
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        for name in ("python3", "python"):
            candidate = Path(virtual_env) / "bin" / name
            if candidate.exists():
                return str(candidate)
    for name in ("python3", "python"):
        candidate = REPO_SRC.parent / ".venv" / "bin" / name
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _wait_for(predicate, description: str, *, timeout: float = 20.0, interval: float = 0.2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {description}.")


def _task_state(project: Path, task_id: str) -> str | None:
    task_path = _task_path(project, task_id)
    if task_path is None:
        return None
    for line in task_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("state:"):
            return line.split(":", 1)[1].strip()
    return None


def _task_path(project: Path, task_id: str) -> Path | None:
    matches = sorted((project / "tasks").glob(f"{task_id}-*.md"))
    return matches[0] if matches else None


def _board_contains(project: Path, section: str, entry: str) -> bool:
    board = (project / "kanban" / "Work.md").read_text(encoding="utf-8")
    if section not in board:
        return False
    return entry in board.split(section, 1)[1]


def _scheduler_stdout(project: RuntimeProject) -> str:
    path = project.runtime_logs / "scheduler-Agent.stdout.log"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _system_log_paths(project: RuntimeProject) -> tuple[Path, ...]:
    paths: list[Path] = []
    job_root = project.root / CONFIG_DIRNAME / "jobs" / "Agent"
    paths.extend(sorted(job_root.glob("*/job.json")))
    paths.extend(sorted((project.project / "events").glob("*.log")))
    paths.extend(sorted((project.project / "events").glob("*.jsonl")))
    paths.extend(sorted(project.runtime_logs.glob("*")))
    for job_path in sorted(job_root.glob("*/job.json")):
        workspace = Path(json.loads(job_path.read_text(encoding="utf-8"))["workspace_path"])
        log_dir = workspace / ".open-tulid" / "logs"
        paths.extend(
            path
            for path in (log_dir / "command.txt", log_dir / "stdout.log", log_dir / "stderr.log")
            if path.exists()
        )
    return tuple(paths)


def _print_system_logs(project: RuntimeProject, capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        print(f"\n--- open-tulid runtime e2e logs: {project.project} ---")
        for path in _system_log_paths(project):
            print(f"[system-log] {path}")
            text = path.read_text(encoding="utf-8")
            if path.name == "job.json":
                payload = json.loads(text)
                print(json.dumps({
                    "job_id": payload.get("job_id"),
                    "status": payload.get("status"),
                    "attempts": payload.get("attempts"),
                    "worker_id": payload.get("worker_id"),
                    "task_id": payload.get("task_id"),
                    "transition_id": payload.get("transition_id"),
                    "metadata": {
                        key: payload.get("metadata", {}).get(key)
                        for key in (
                            "completion_endpoint",
                            "completion_endpoint_host",
                            "completion_endpoint_port",
                            "failure_reason",
                            "worker_returncode",
                        )
                        if key in payload.get("metadata", {})
                    },
                }, sort_keys=True, indent=2))
                continue
            print(text.rstrip() or "<empty>")
        print("[trusted-state] board")
        print((project.project / "kanban" / "Work.md").read_text(encoding="utf-8").rstrip())
        for task_path in sorted((project.project / "tasks").glob("*.md")):
            print(f"[trusted-state] {task_path}")
            print(task_path.read_text(encoding="utf-8").rstrip())
        print("--- end open-tulid runtime e2e logs ---")


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ("docker", "version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _safe_tag(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", value).lower()
