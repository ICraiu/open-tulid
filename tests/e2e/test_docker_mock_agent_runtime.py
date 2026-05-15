from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_tulid.cli.main import app
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME
from open_tulid.runtime import JsonlEventStore


TASK_ID = "01J00000000000000000000001"
FIXTURES = Path(__file__).parent / "fixtures"

runner = CliRunner()


@dataclass(frozen=True)
class E2EProject:
    root: Path
    vault: Path
    project: Path
    repo: Path
    workflow: Path


@pytest.fixture(scope="session")
def docker_mock_agent_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    if not _docker_available():
        pytest.skip("Docker daemon is not available for Docker-backed E2E tests.")

    context = tmp_path_factory.mktemp("open-tulid-mock-agent-image")
    tag = f"open-tulid/e2e-mock-agent:{_safe_tag(context.name)}"
    shutil.copytree(FIXTURES / "mock-agent", context, dirs_exist_ok=True)

    built = subprocess.run(
        ("docker", "build", "-t", tag, str(context)),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if built.returncode != 0:
        pytest.skip(f"Cannot build Docker mock agent image: {built.stderr.strip()}")

    yield tag

    subprocess.run(("docker", "rmi", "-f", tag), check=False, capture_output=True, text=True)


def test_run_one_with_docker_mock_agent_accepts_and_moves_task(
    tmp_path: Path,
    docker_mock_agent_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _make_e2e_project(
        tmp_path,
        image=docker_mock_agent_image,
        scenario="accept_first_try",
        task_body="Implement a health check.",
    )

    with _cwd(project.root):
        result = runner.invoke(app, ["jobs", "run-one", "Agent"])

    assert result.exit_code == 0, result.output
    assert "Worker exited with code 0." in result.output
    _assert_task_moved_to_code_review(project.project)
    _assert_job_accepted(project.project)
    _assert_artifacts_promoted(project.project)
    _assert_container_logs(project.project, "mock agent scenario=accept_first_try")
    assert [event.event_type for event in JsonlEventStore(project.project / "events").iter_events()] == [
        "TransitionAccepted",
        "ExecutionJobCreated",
        "ExecutionStarted",
        "ExecutionCompletionSubmitted",
        "TransitionAccepted",
        "TaskMoved",
        "ArtifactWritten",
        "ArtifactWritten",
        "ReviewRequested",
        "ExecutionFinished",
    ]
    _print_system_logs(project.project, capsys)


def test_run_one_with_docker_mock_agent_fixes_rejected_completion(
    tmp_path: Path,
    docker_mock_agent_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _make_e2e_project(
        tmp_path,
        image=docker_mock_agent_image,
        scenario="reject_then_fix",
        task_body="Reject once, then fix the output.",
    )

    with _cwd(project.root):
        result = runner.invoke(app, ["jobs", "run-one", "Agent"])

    assert result.exit_code == 0, result.output
    _assert_task_moved_to_code_review(project.project)
    _assert_job_accepted(project.project)
    _assert_container_logs(project.project, "completion status=400")
    _assert_container_logs(project.project, "completion status=200")
    event_types = [event.event_type for event in JsonlEventStore(project.project / "events").iter_events()]
    assert "ExecutionCompletionRejected" in event_types
    assert event_types[-1] == "ExecutionFinished"
    _print_system_logs(project.project, capsys)


def test_run_one_with_docker_mock_agent_fails_when_agent_never_completes(
    tmp_path: Path,
    docker_mock_agent_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _make_e2e_project(
        tmp_path,
        image=docker_mock_agent_image,
        scenario="no_completion",
        task_body="Exit without submitting completion.",
    )

    with _cwd(project.root):
        result = runner.invoke(app, ["jobs", "run-one", "Agent"])

    assert result.exit_code == 0, result.output
    payload = _job_payload(project.project)
    assert payload["status"] == "failed"
    assert payload["metadata"]["failure_reason"] == "completion_not_accepted"
    board = (project.project / "kanban" / "Work.md").read_text(encoding="utf-8")
    assert f"## Todo\n- [ ] [[{TASK_ID}-healthz]]" in board
    assert [event.event_type for event in JsonlEventStore(project.project / "events").iter_events()][-1] == "ExecutionFailed"
    _print_system_logs(project.project, capsys)


def _make_e2e_project(
    tmp_path: Path,
    *,
    image: str,
    scenario: str,
    task_body: str,
) -> E2EProject:
    root = tmp_path / "workspace"
    vault = root / "vault"
    project = vault / "Agent"
    repo = root / "repo"
    workflow = root / "workflow.yaml"
    fixture_project = FIXTURES / "project"
    shutil.copytree(fixture_project / "Agent", project)
    shutil.copytree(fixture_project / "repo", repo)
    shutil.copy2(fixture_project / "workflow.yaml", workflow)
    (root / CONFIG_DIRNAME).mkdir(parents=True)
    task_path = project / "tasks" / f"{TASK_ID}-healthz.md"
    task_path.write_text(
        task_path.read_text(encoding="utf-8").format(task_body=task_body),
        encoding="utf-8",
    )
    config_template = (fixture_project / "open-tulid.toml.template").read_text(encoding="utf-8")
    (root / CONFIG_DIRNAME / CONFIG_FILENAME).write_text(
        config_template.format(
            vault_root=vault,
            repo_root=repo,
            workflow_path=workflow,
            mock_agent_image=image,
            mock_agent_scenario=scenario,
            prompt_packet="{prompt_packet}",
        ),
        encoding="utf-8",
    )
    return E2EProject(root=root, vault=vault, project=project, repo=repo, workflow=workflow)


def _assert_task_moved_to_code_review(project: Path) -> None:
    board = (project / "kanban" / "Work.md").read_text(encoding="utf-8")
    assert "## Todo\n\n## Code review\n" in board
    assert f"- [ ] [[{TASK_ID}-healthz]]" in board.split("## Code review", 1)[1]
    task = (project / "tasks" / f"{TASK_ID}-healthz.md").read_text(encoding="utf-8")
    assert "state: CodeReview" in task


def _assert_job_accepted(project: Path) -> None:
    payload = _job_payload(project)
    assert payload["status"] == "accepted"
    assert payload["attempts"] == 1
    assert payload["metadata"]["completion_endpoint"].startswith("http://host.docker.internal:")


def _assert_artifacts_promoted(project: Path) -> None:
    assert (project / "artifacts" / TASK_ID / "ImplementationSummary" / "implementation-summary.md").is_file()
    assert (project / "artifacts" / TASK_ID / "TestResult" / "test-result.md").is_file()
    task = (project / "tasks" / f"{TASK_ID}-healthz.md").read_text(encoding="utf-8")
    assert "artifact_links:" in task


def _assert_container_logs(project: Path, expected: str) -> None:
    job_files = list((project / "jobs").glob("*/job.json"))
    assert len(job_files) == 1
    workspace = Path(json.loads(job_files[0].read_text(encoding="utf-8"))["workspace_path"])
    stdout = (workspace / ".open-tulid" / "logs" / "stdout.log").read_text(encoding="utf-8")
    assert expected in stdout


def _job_payload(project: Path) -> dict[str, object]:
    job_files = list((project / "jobs").glob("*/job.json"))
    assert len(job_files) == 1
    return json.loads(job_files[0].read_text(encoding="utf-8"))


def _print_system_logs(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        print(f"\n--- open-tulid e2e system logs: {project} ---")
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
            if path.suffix == ".jsonl":
                for line in text.splitlines():
                    event = json.loads(line)
                    print(json.dumps({
                        "event_type": event.get("event_type"),
                        "job_id": event.get("job_id"),
                        "task_id": event.get("task_id"),
                        "transition_id": event.get("transition_id"),
                        "data": event.get("data"),
                    }, sort_keys=True))
                continue
            print(text.rstrip() or "<empty>")
        _print_trusted_task_state(project)
        print("--- end open-tulid e2e system logs ---")


def _system_log_paths(project: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    paths.extend(sorted((project / "jobs").glob("*/job.json")))
    paths.extend(sorted((project / "events").glob("*.log")))
    paths.extend(sorted((project / "events").glob("*.jsonl")))
    for job_path in sorted((project / "jobs").glob("*/job.json")):
        workspace = Path(json.loads(job_path.read_text(encoding="utf-8"))["workspace_path"])
        log_dir = workspace / ".open-tulid" / "logs"
        paths.extend(path for path in (
            log_dir / "command.txt",
            log_dir / "stdout.log",
            log_dir / "stderr.log",
        ) if path.exists())
    return tuple(paths)


def _print_trusted_task_state(project: Path) -> None:
    task_path = project / "tasks" / f"{TASK_ID}-healthz.md"
    board_path = project / "kanban" / "Work.md"
    print(f"[trusted-state] {task_path}")
    print(_frontmatter_state(task_path.read_text(encoding="utf-8")))
    print(f"[trusted-state] {board_path}")
    print(board_path.read_text(encoding="utf-8").rstrip())


def _frontmatter_state(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("state:"):
            return line
    return "state: <missing>"


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


class _cwd:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.original = Path.cwd()

    def __enter__(self) -> None:
        self.original = Path.cwd()
        os.chdir(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        os.chdir(self.original)
