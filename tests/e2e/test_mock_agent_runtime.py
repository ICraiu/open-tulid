from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from open_tulid.cli.main import app
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME
from open_tulid.containers.runtime import AgentRunRequest, AgentRunResult
from open_tulid.runtime import JsonlEventStore


TASK_ID = "01J00000000000000000000001"

runner = CliRunner()


@dataclass(frozen=True)
class E2EProject:
    root: Path
    vault: Path
    project: Path
    repo: Path
    workflow: Path


class MockCodingAgent:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.submissions: list[dict[str, Any]] = []

    def run(self, request: AgentRunRequest, *, docker_executable: str) -> AgentRunResult:
        context = json.loads((request.workspace / ".open-tulid" / "job-context.json").read_text(encoding="utf-8"))
        prompt = (request.workspace / ".open-tulid" / "prompt-packet.md").read_text(encoding="utf-8")
        assert context["task_id"] == TASK_ID
        assert "Open Tulid Job" in prompt
        assert request.env["OPEN_TULID_COMPLETION_ENDPOINT"] == context["completion_endpoint"]

        if self.scenario == "no_completion":
            return AgentRunResult(
                agent_id=request.agent_id,
                image=request.image,
                command=("mock-coding-agent", self.scenario),
                returncode=0,
                stdout="mock agent exited without completion\n",
            )

        if self.scenario == "reject_then_fix":
            self._submit(request, {
                "submission_id": "first",
                "attempt": 1,
                "summary": "not done yet",
                "artifacts": [],
                "changed_files": [],
                "validation_evidence": {"tests_pass": "not run"},
            }, expected_status=400)

        output = request.workspace / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "implementation-summary.md").write_text(
            "# Implementation Summary\n\nMock agent completed the task.\n",
            encoding="utf-8",
        )
        (output / "test-result.md").write_text(
            "# Test Result\n\npytest passed in mock agent.\n",
            encoding="utf-8",
        )
        (request.workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")
        self._submit(request, {
            "submission_id": "accepted",
            "attempt": 2 if self.scenario == "reject_then_fix" else 1,
            "summary": "implemented by mock agent",
            "artifacts": [
                {"type": "ImplementationSummary", "path": "implementation-summary.md"},
                {"type": "TestResult", "path": "test-result.md"},
            ],
            "changed_files": ["app.py"],
            "validation_evidence": {"tests_pass": "passed"},
        }, expected_status=200)
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("mock-coding-agent", self.scenario),
            returncode=0,
            stdout=f"mock agent scenario={self.scenario}\n",
        )

    def _submit(self, request: AgentRunRequest, payload: dict[str, Any], *, expected_status: int) -> None:
        self.submissions.append(payload)
        http_request = urllib.request.Request(
            str(request.env["OPEN_TULID_COMPLETION_ENDPOINT"]),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-open-tulid-completion-token": str(request.env["OPEN_TULID_COMPLETION_TOKEN"]),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=5) as response:
                status = response.status
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8")
        assert status == expected_status, body


def test_run_one_end_to_end_with_mock_agent_accepts_and_moves_task(tmp_path: Path, monkeypatch):
    project = _make_e2e_project(tmp_path, task_body="Implement a health check.")
    mock_agent = MockCodingAgent("accept_first_try")
    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", mock_agent.run)

    with _cwd(project.root):
        result = runner.invoke(app, ["jobs", "run-one", "Agent"])

    assert result.exit_code == 0, result.output
    assert "Worker exited with code 0." in result.output
    assert len(mock_agent.submissions) == 1
    _assert_task_moved_to_code_review(project.project)
    _assert_job_accepted(project.project)
    _assert_artifacts_promoted(project.project)
    event_types = [event.event_type for event in JsonlEventStore(project.project / "events").iter_events()]
    assert event_types == [
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


def test_run_one_end_to_end_mock_agent_fixes_rejected_completion(tmp_path: Path, monkeypatch):
    project = _make_e2e_project(tmp_path, task_body="Reject once then fix.")
    mock_agent = MockCodingAgent("reject_then_fix")
    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", mock_agent.run)

    with _cwd(project.root):
        result = runner.invoke(app, ["jobs", "run-one", "Agent"])

    assert result.exit_code == 0, result.output
    assert len(mock_agent.submissions) == 2
    _assert_task_moved_to_code_review(project.project)
    _assert_job_accepted(project.project)
    event_types = [event.event_type for event in JsonlEventStore(project.project / "events").iter_events()]
    assert "ExecutionCompletionRejected" in event_types
    assert event_types[-1] == "ExecutionFinished"


def test_run_one_end_to_end_fails_when_mock_agent_never_completes(tmp_path: Path, monkeypatch):
    project = _make_e2e_project(tmp_path, task_body="Exit without completing.")
    mock_agent = MockCodingAgent("no_completion")
    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", mock_agent.run)

    with _cwd(project.root):
        result = runner.invoke(app, ["jobs", "run-one", "Agent"])

    assert result.exit_code == 0, result.output
    assert len(mock_agent.submissions) == 0
    job_files = list((project.project / "jobs").glob("*/job.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["metadata"]["failure_reason"] == "completion_not_accepted"
    board = (project.project / "kanban" / "Work.md").read_text(encoding="utf-8")
    assert f"## Todo\n- [ ] [[{TASK_ID}-healthz]]" in board
    event_types = [event.event_type for event in JsonlEventStore(project.project / "events").iter_events()]
    assert event_types[-1] == "ExecutionFailed"


def _make_e2e_project(tmp_path: Path, *, task_body: str) -> E2EProject:
    root = tmp_path / "workspace"
    vault = root / "vault"
    project = vault / "Agent"
    repo = root / "repo"
    workflow = root / "workflow.yaml"
    (root / CONFIG_DIRNAME).mkdir(parents=True)
    (project / "tasks").mkdir(parents=True)
    (project / "kanban").mkdir(parents=True)
    (project / "agents").mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("def healthz():\n    raise NotImplementedError\n", encoding="utf-8")
    (project / "agents" / "default.agent.md").write_text(
        "# Default Agent\n\nUse pytest and submit completion evidence.\n",
        encoding="utf-8",
    )
    (project / "tasks" / f"{TASK_ID}-healthz.md").write_text(
        "---\n"
        f"id: {TASK_ID}\n"
        "type: CodingTask\n"
        "state: Todo\n"
        "---\n"
        "\n"
        "# Add health check\n"
        "\n"
        f"{task_body}\n",
        encoding="utf-8",
    )
    (project / "kanban" / "Work.md").write_text(
        "## Todo\n"
        f"- [ ] [[{TASK_ID}-healthz]]\n"
        "\n"
        "## Code review\n",
        encoding="utf-8",
    )
    workflow.write_text(_workflow_yaml(), encoding="utf-8")
    (root / CONFIG_DIRNAME / CONFIG_FILENAME).write_text(
        "[vault]\n"
        f'root = "{vault}"\n'
        'projects = ["Agent"]\n'
        "\n"
        "[projects.Agent]\n"
        'tracker_path = "Agent"\n'
        f'repo_root = "{repo}"\n'
        "\n"
        "[workflow]\n"
        f'path = "{workflow}"\n'
        "\n"
        "[runtime]\n"
        'completion_host = "127.0.0.1"\n'
        'completion_container_host = "127.0.0.1"\n'
        "default_timeout_seconds = 30\n"
        "\n"
        "[runtime.worker_args]\n"
        'codex = ["exec", "{prompt_packet}"]\n',
        encoding="utf-8",
    )
    return E2EProject(root=root, vault=vault, project=project, repo=repo, workflow=workflow)


def _workflow_yaml() -> str:
    return (
        "schema_version: 1\n"
        "storage:\n"
        "  obsidian:\n"
        "    boards:\n"
        "      Work: kanban/Work.md\n"
        "    state_mappings:\n"
        "      - state: Todo\n"
        "        board: Work\n"
        "        column: Todo\n"
        "      - state: CodeReview\n"
        "        board: Work\n"
        "        column: Code review\n"
        "statements:\n"
        "  - kind: state\n"
        "    id: Todo\n"
        "  - kind: state\n"
        "    id: CodeReview\n"
        "  - kind: task_type\n"
        "    id: CodingTask\n"
        "  - kind: artifact_type\n"
        "    id: ImplementationSummary\n"
        "    template: implementation-summary.md\n"
        "  - kind: artifact_type\n"
        "    id: TestResult\n"
        "    template: test-result.md\n"
        "  - kind: validation_type\n"
        "    id: tests_pass\n"
        "  - kind: worker\n"
        "    id: codex\n"
        "    type: codex\n"
        "  - kind: transition\n"
        "    id: ImplementTask\n"
        "    task_type: CodingTask\n"
        "    from: Todo\n"
        "    to: CodeReview\n"
        "    worker: codex\n"
        "    default_for_scheduler: true\n"
        "    requires:\n"
        "      artifacts:\n"
        "        - ImplementationSummary\n"
        "        - TestResult\n"
        "      validations:\n"
        "        - type: tests_pass\n"
    )


def _assert_task_moved_to_code_review(project: Path) -> None:
    board = (project / "kanban" / "Work.md").read_text(encoding="utf-8")
    assert "## Todo\n\n## Code review\n" in board
    assert f"- [ ] [[{TASK_ID}-healthz]]" in board.split("## Code review", 1)[1]
    task = (project / "tasks" / f"{TASK_ID}-healthz.md").read_text(encoding="utf-8")
    assert "state: CodeReview" in task


def _assert_job_accepted(project: Path) -> None:
    job_files = list((project / "jobs").glob("*/job.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert payload["status"] == "accepted"
    assert payload["attempts"] == 1
    assert payload["metadata"]["completion_endpoint"].startswith("http://127.0.0.1:")


def _assert_artifacts_promoted(project: Path) -> None:
    assert (project / "artifacts" / TASK_ID / "ImplementationSummary" / "implementation-summary.md").is_file()
    assert (project / "artifacts" / TASK_ID / "TestResult" / "test-result.md").is_file()
    task = (project / "tasks" / f"{TASK_ID}-healthz.md").read_text(encoding="utf-8")
    assert "artifact_links:" in task


class _cwd:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.original = Path.cwd()

    def __enter__(self) -> None:
        self.original = Path.cwd()
        os.chdir(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        os.chdir(self.original)
