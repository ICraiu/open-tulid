from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import pytest

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.containers.runtime import AgentRunResult, ContainerMount
from open_tulid.domain import (
    DerivesDefinition,
    DomainError,
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    ValidationCallDefinition,
    WorkerDefinition,
    WorkflowDefinition,
)
from open_tulid.models import ModelProxyConfig, ProjectConfig, ResourceConfig, RuntimeConfig
from open_tulid.runtime import FileExecutionJobStore, FileResourceLeaseStore, JobExecutor, JsonlEventStore
from open_tulid.runtime.executor import _build_runtime_prompt
from open_tulid.workflow.implementations import VALIDATION_IMPLEMENTATIONS, WorkflowExecutionContext
from socket_utils import can_bind_localhost


TASK_ID = "01J00000000000000000000001"
JOB_ID = "01J00000000000000000000JOB"


@dataclass(frozen=True)
class _FakeCompletionEndpoint:
    job_id: str
    host: str = "127.0.0.1"
    port: int = 1

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:1/jobs/{self.job_id}/complete"

    def stop(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fake_completion_endpoint_when_sockets_unavailable(request, monkeypatch):
    if can_bind_localhost():
        return
    if request.node.name == "test_executor_serves_completion_endpoint_and_accepts_before_worker_exit":
        pytest.skip("localhost socket binding is unavailable in this sandbox")
    monkeypatch.setattr(
        "open_tulid.runtime.executor.JobExecutor._start_completion_endpoint",
        lambda self, job_id: _FakeCompletionEndpoint(job_id),
    )


@dataclass
class FakeAdapter:
    moved_to: str | None = None
    name: str = "fake"
    capabilities: frozenset[AdapterCapability] = frozenset({
        AdapterCapability.LOAD_PROJECT,
        AdapterCapability.READ_TASK,
        AdapterCapability.MOVE_TASK,
    })

    def load_project(self) -> LoadProjectResult:
        task = _task()
        return LoadProjectResult(snapshot=ProjectSnapshot(
            project_id="Agent",
            tasks=MappingProxyType({task.id: task}),
            board_positions=MappingProxyType({}),
        ))

    def read_task(self, task_id: str) -> ReadTaskResult:
        return ReadTaskResult(task=_task()) if task_id == TASK_ID else ReadTaskResult()

    def write_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        self.moved_to = state
        return WriteResult(path=state)

    def append_event(self, event: Mapping[str, Any]) -> WriteResult:
        return WriteResult(path="events/test.jsonl")


def test_build_runtime_prompt_for_non_artifact_transition_marks_output_context_read_only():
    prompt = _build_runtime_prompt(
        job_id=JOB_ID,
        task_title="Implement CLI",
        task_body="Do the implementation work.",
        transition_id="ImplementTask",
        from_state="Todo",
        to_state="SelfReview1",
        required_artifacts=(),
        required_validations=("tests_pass",),
        required_validation_details=("tests_pass: run `npm test`",),
        changed_files_required=True,
        derived_artifact_type=None,
        completion_endpoint="http://127.0.0.1/jobs/test/complete",
    )

    assert "## Role" in prompt
    assert "You are implementing one already-derived scoped task inside an existing plan." in prompt
    assert "## Primary Objective" in prompt
    assert "Success for this transition is not producing new planning artifacts." in prompt
    assert "## Context Priority" in prompt
    assert "The current task body is the authoritative scope boundary for this job." in prompt
    assert "## Read-Only And Writable Paths" in prompt
    assert "No artifacts are required for this transition. Submit an empty `artifacts` array." in prompt
    assert "Treat existing files under `output/` as read-only context" in prompt
    assert "Do not regenerate product specs, technical directions, implementation specs, or task breakdown files" in prompt
    assert "This implementation transition does not require artifacts, so leave `output/` alone unless Tulid explicitly requires it." in prompt
    assert "## Completion Contract" in prompt
    assert "Required validation commands:" in prompt
    assert "- tests_pass: run `npm test`" in prompt
    assert "`changed_files` is required for this transition." in prompt
    assert "Completion is not implied by process exit code or workspace edits alone." in prompt
    assert "ULTRA IMPORTANT: when ready, submit completion evidence with `curl`." in prompt
    assert "curl -sS -X POST \\" in prompt
    assert "--data-binary @- <<'JSON'" in prompt
    assert '"artifacts": [],' in prompt


def test_build_runtime_prompt_for_planning_transition_uses_planning_framing():
    prompt = _build_runtime_prompt(
        job_id=JOB_ID,
        task_title="Write implementation spec",
        task_body="Produce the implementation spec.",
        transition_id="WriteImplementationSpec",
        from_state="DirectionApproved",
        to_state="SpecReady",
        required_artifacts=("ImplementationSpec",),
        required_validations=(),
        required_validation_details=(),
        changed_files_required=False,
        derived_artifact_type=None,
        completion_endpoint="http://127.0.0.1/jobs/test/complete",
    )

    assert "## Role" in prompt
    assert "You are executing a planning or artifact-producing workflow transition for this project." in prompt
    assert "## Context Priority" in prompt
    assert "The transition objective and required artifacts define the deliverable for this job." in prompt
    assert "## Read-Only And Writable Paths" in prompt
    assert "If repository files are present in the workspace, inspect them" in prompt
    assert "Treat repository source files as read-only context for planning transitions" in prompt
    assert "Write required completion artifacts under `output/`." in prompt
    assert "Only create the artifact files explicitly required for this transition." in prompt


def test_build_runtime_prompt_for_derived_transition_requires_artifact_submission_per_file():
    prompt = _build_runtime_prompt(
        job_id=JOB_ID,
        task_title="Break down spec",
        task_body="Generate child tasks.",
        transition_id="BreakDownImplementationSpec",
        from_state="ReadyForBreakdown",
        to_state="Done",
        required_artifacts=(),
        required_validations=(),
        required_validation_details=(),
        changed_files_required=False,
        derived_artifact_type="ImplementationTaskFile",
        completion_endpoint="http://127.0.0.1/jobs/test/complete",
    )

    assert "You are executing a planning or artifact-producing workflow transition for this project." in prompt
    assert "Submit one artifact entry per generated `ImplementationTaskFile` file." in prompt
    assert "Only submitted derived-task artifacts will be promoted and turned into tasks." in prompt


def test_executor_serves_completion_endpoint_and_accepts_before_worker_exit(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    adapter = FakeAdapter()
    seen: dict[str, object] = {}

    def fake_run_agent_container(request, *, docker_executable):
        seen["args"] = request.args
        seen["endpoint"] = request.env["OPEN_TULID_COMPLETION_ENDPOINT"]
        seen["prompt"] = request.env["OPEN_TULID_PROMPT_PACKET"]
        output = Path(request.workspace) / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.md").write_text("done\n", encoding="utf-8")
        payload = json.dumps({
            "summary": "done",
            "artifacts": [{"type": "result.md", "path": "result.md"}],
            "changed_files": [],
            "validation_evidence": {},
        }).encode("utf-8")
        http_request = urllib.request.Request(
            str(request.env["OPEN_TULID_COMPLETION_ENDPOINT"]),
            data=payload,
            headers={
                "content-type": "application/json",
                "x-open-tulid-completion-token": "secret",
            },
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=5) as response:
            seen["status"] = response.status
            seen["response"] = json.loads(response.read())
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=17,
        )

    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        fake_run_agent_container,
    )

    executor = JobExecutor(
        workflow=_workflow(),
        adapter=adapter,
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_args={"codex": ("exec", "{prompt_packet}")},
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    assert seen["status"] == 200
    assert seen["response"] == {"accepted": True, "next_state": "CodeReview"}
    assert seen["args"] == ("exec", "/workspace/project/.open-tulid/prompt-packet.md")
    assert str(seen["endpoint"]).startswith("http://127.0.0.1:")
    assert seen["prompt"] == "/workspace/project/.open-tulid/prompt-packet.md"
    assert (workspace / ".open-tulid" / "prompt-packet.md").is_file()
    prompt = (workspace / ".open-tulid" / "prompt-packet.md").read_text(encoding="utf-8")
    assert "## Final Required Step" in prompt
    assert "ULTRA IMPORTANT: before exiting successfully, submit completion evidence with `curl` exactly as shown below." in prompt
    assert "curl -sS -X POST \\" in prompt
    assert prompt.rstrip().endswith("A zero exit code without this curl completion submission is a failed Tulid job.")
    assert adapter.moved_to == "CodeReview"
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "accepted"


def test_executor_fails_successful_worker_without_explicit_completion_evidence(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    workflow = _workflow_without_requirements()
    adapter = FakeAdapter()

    def fake_run_agent_container(request, *, docker_executable):
        (Path(request.workspace) / "src").mkdir(parents=True, exist_ok=True)
        (Path(request.workspace) / "src" / "app.py").write_text("print('done')\n", encoding="utf-8")
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=0,
        )

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)

    executor = JobExecutor(
        workflow=workflow,
        adapter=adapter,
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    assert adapter.moved_to is None
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "failed"
    assert loaded.job.metadata["failure_reason"] == "completion_not_accepted"


def test_executor_preserves_terminal_failed_status_set_during_worker_run(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    adapter = FakeAdapter()
    events = JsonlEventStore(tmp_path / "events")

    def fake_run_agent_container(request, *, docker_executable):
        store.update_status(
            JOB_ID,
            "failed",
            metadata={"failure_reason": "validation_failed"},
        )
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=0,
        )

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)

    executor = JobExecutor(
        workflow=_workflow_without_requirements(),
        adapter=adapter,
        job_store=store,
        event_store=events,
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "failed"
    assert loaded.job.metadata["failure_reason"] == "validation_failed"
    assert [event.event_type for event in events.iter_events()] == ["ExecutionStarted"]


def test_executor_waits_for_submitted_completion_to_settle_after_worker_exit(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    events = JsonlEventStore(tmp_path / "events")

    def fake_run_agent_container(request, *, docker_executable):
        assert store.update_status(JOB_ID, "completion_submitted").accepted is True

        def accept_later() -> None:
            assert store.update_status(JOB_ID, "accepted").accepted is True

        timer = threading.Timer(0.1, accept_later)
        timer.start()
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=0,
        )

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)

    executor = JobExecutor(
        workflow=_workflow_without_requirements(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=events,
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
        completion_settle_timeout_seconds=2,
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "accepted"
    assert [event.event_type for event in events.iter_events()] == ["ExecutionStarted"]


def test_executor_requires_explicit_completion_call_even_when_workspace_changes(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "app.py").write_text("print('before')\n", encoding="utf-8")
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    adapter = FakeAdapter()

    def fake_run_agent_container(request, *, docker_executable):
        (Path(request.workspace) / "src" / "app.py").write_text("print('after')\n", encoding="utf-8")
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=0,
        )

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)

    executor = JobExecutor(
        workflow=_workflow_without_requirements(),
        adapter=adapter,
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent", repo_root=repo_root),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    assert adapter.moved_to is None
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "failed"
    assert loaded.job.metadata["failure_reason"] == "completion_not_accepted"
    assert (repo_root / "src" / "app.py").read_text(encoding="utf-8") == "print('before')\n"


def test_executor_does_not_run_trusted_validations_without_completion_call(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "app.py").write_text("print('before')\n", encoding="utf-8")
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    adapter = FakeAdapter()

    def fake_run_agent_container(request, *, docker_executable):
        (Path(request.workspace) / "app.py").write_text("print('after')\n", encoding="utf-8")
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=0,
        )

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)
    workflow = WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "CodeReview": StateDefinition(id="CodeReview"),
        }),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex",
                requires=RequirementDefinition(
                    validations=(ValidationCallDefinition(
                        type="tests_pass",
                        args=MappingProxyType({"command": ("python", "-c", "import pathlib; pathlib.Path('app.py').read_text()")}),
                    ),),
                ),
                transaction=None,
            ),
        }),
    )

    executor = JobExecutor(
        workflow=workflow,
        adapter=adapter,
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent", repo_root=repo_root),
        validation_implementations={"tests_pass": VALIDATION_IMPLEMENTATIONS["tests_pass"]},
        validation_context_factory=lambda workspace, output_root: WorkflowExecutionContext(
            project_root=workspace,
            vault_root=output_root,
        ),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    assert adapter.moved_to is None
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "failed"
    assert loaded.job.metadata["failure_reason"] == "completion_not_accepted"
    assert (repo_root / "app.py").read_text(encoding="utf-8") == "print('before')\n"


def test_executor_injects_linked_context_and_instructions(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    (project / "artifacts").mkdir(parents=True)
    (project / "docs").mkdir()
    (project / "agents").mkdir()
    (project / "artifacts" / "spec.md").write_text("Spec sees [[extra]].\n", encoding="utf-8")
    (project / "docs" / "extra.md").write_text("Extra context.\n", encoding="utf-8")
    (project / "docs" / "parent-extra.md").write_text("Parent extra context.\n", encoding="utf-8")
    (project / "agents" / "default.agent.md").write_text("Default instructions.\n", encoding="utf-8")
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True

    class ProjectAdapter(FakeAdapter):
        config = type("Cfg", (), {"project_root": project})()

        def read_task(self, task_id: str) -> ReadTaskResult:
            if task_id == TASK_ID:
                return ReadTaskResult(task=Task(
                    id=TASK_ID,
                    title="Task",
                    path="tasks/task.md",
                    current_state="Todo",
                    task_type="task",
                    artifact_links=("artifacts/spec.md",),
                    parent_id="parent-task",
                    body="Task body.",
                ))
            if task_id == "parent-task":
                return ReadTaskResult(task=Task(
                    id="parent-task",
                    title="Parent Task",
                    path="tasks/parent.md",
                    current_state="Done",
                    task_type="task",
                    body="Parent body sees [[parent-extra]].",
                ))
            return ReadTaskResult()

    def fake_run_agent_container(request, *, docker_executable):
        output = Path(request.workspace) / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.md").write_text("done\n", encoding="utf-8")
        prompt = (Path(request.workspace) / ".open-tulid" / "prompt-packet.md").read_text(encoding="utf-8")
        assert "Task body." in prompt
        assert "## Parent Context 1" in prompt
        assert "background project context, not an instruction to broaden the assigned task" in prompt
        assert "Parent body sees" in prompt
        assert "Spec sees" in prompt
        assert "Extra context." in prompt
        assert "Parent extra context." in prompt
        assert "Linked Reference Context: artifacts/spec.md" in prompt
        assert "supports implementation decisions but does not redefine the assigned task scope" in prompt
        assert "Default instructions." in prompt
        return AgentRunResult(agent_id=request.agent_id, image=request.image, command=("fake",), returncode=17)

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)
    monkeypatch.setattr("open_tulid.runtime.executor._write_run_logs", lambda workspace, result: None)
    monkeypatch.setattr(
        JobExecutor,
        "_start_completion_endpoint",
        lambda self, job_id: type(
            "Endpoint",
            (),
            {
                "url": f"http://127.0.0.1/jobs/{job_id}/complete",
                "host": "127.0.0.1",
                "port": 0,
                "stop": lambda self: None,
            },
        )(),
    )
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=ProjectAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True


def test_executor_strips_generated_derived_task_sections_and_guides_multi_artifact_derivation(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    (project / "artifacts" / TASK_ID / "result.md").mkdir(parents=True)
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "real-context.md").write_text("Real context.\n", encoding="utf-8")
    (project / "tasks").mkdir()
    (project / "tasks" / "stale-child.md").write_text("Stale child task.\n", encoding="utf-8")
    (project / "agents").mkdir()
    (project / "agents" / "default.agent.md").write_text("Default instructions.\n", encoding="utf-8")
    (project / "artifacts" / TASK_ID / "result.md" / "old.md").write_text("Old result artifact.\n", encoding="utf-8")
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True

    class ProjectAdapter(FakeAdapter):
        config = type("Cfg", (), {"project_root": project})()

        def read_task(self, task_id: str) -> ReadTaskResult:
            if task_id != TASK_ID:
                return ReadTaskResult()
            return ReadTaskResult(task=Task(
                id=TASK_ID,
                title="Task",
                path="tasks/task.md",
                current_state="Todo",
                task_type="task",
                artifact_links=("artifacts/01J00000000000000000000001/result.md/old.md",),
                body="See [[real-context]].\n\n## Derived tasks\n- [[stale-child]]\n",
            ))

    seen: dict[str, str] = {}

    def fake_run_agent_container(request, *, docker_executable):
        prompt = (Path(request.workspace) / ".open-tulid" / "prompt-packet.md").read_text(encoding="utf-8")
        context = json.loads((Path(request.workspace) / ".open-tulid" / "job-context.json").read_text(encoding="utf-8"))
        seen["prompt"] = prompt
        seen["body"] = context["task"]["body"]
        return AgentRunResult(agent_id=request.agent_id, image=request.image, command=("fake",), returncode=1)

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)
    workflow = WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "Done": StateDefinition(id="Done"),
        }),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="Done",
                worker="codex",
                requires=RequirementDefinition(artifacts=("result.md",)),
                derives=DerivesDefinition(task_type="child", state="Todo", artifact_type="ImplementationTaskFile"),
                transaction=None,
            ),
        }),
    )
    executor = JobExecutor(
        workflow=workflow,
        adapter=ProjectAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    assert "## Derived tasks" not in seen["prompt"]
    assert "stale-child" not in seen["prompt"]
    assert "Stale child task." not in seen["prompt"]
    assert "Old result artifact." not in seen["prompt"]
    assert "See [[real-context]]." in seen["prompt"]
    assert "Real context." in seen["prompt"]
    assert "Submit one artifact entry per generated `ImplementationTaskFile` file." in seen["prompt"]
    assert "Only submitted derived-task artifacts will be promoted and turned into tasks." in seen["prompt"]
    assert "## Derived tasks" not in seen["body"]


def test_executor_uses_worker_implementation_for_container_image_and_fallback_args(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex_direction",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    seen = {}
    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        lambda request, *, docker_executable: seen.update({
            "agent_id": request.agent_id,
            "image": request.image,
            "args": request.args,
        }) or AgentRunResult(agent_id=request.agent_id, image=request.image, command=("fake",), returncode=1),
    )
    workflow = _workflow()
    workflow = WorkflowDefinition(
        schema_version=workflow.schema_version,
        states=workflow.states,
        task_types=workflow.task_types,
        artifact_types=workflow.artifact_types,
        validation_types=workflow.validation_types,
        operation_types=workflow.operation_types,
        workers=MappingProxyType({
            "codex_direction": WorkerDefinition(
                id="codex_direction",
                type="codex",
                implementation_id="codex",
            ),
        }),
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex_direction",
                requires=RequirementDefinition(artifacts=("result.md",)),
                transaction=None,
            ),
        }),
    )
    executor = JobExecutor(
        workflow=workflow,
        adapter=FakeAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_args={"codex": ("exec", "{prompt_packet}")},
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    assert seen["agent_id"] == "codex"
    assert seen["image"] == "open-tulid/agent-codex:latest"
    assert seen["args"] == ("exec", "/workspace/project/.open-tulid/prompt-packet.md")


def test_executor_releases_resource_lease_when_worker_fails(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    leases = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )

    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        lambda request, *, docker_executable: AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=("fake",),
            returncode=17,
        ),
    )
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_resources={"codex": ("local-llm",)},
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
        lease_store=leases,
    )

    result = executor.run(JOB_ID)

    assert result.accepted is True
    assert leases.leases_for("local-llm") == ()


def test_executor_redacts_scoped_tokens_from_persisted_command_log(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "completion-secret"},
    )).accepted is True
    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        lambda request, *, docker_executable: AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=(
                "docker",
                "run",
                "-e",
                "OPEN_TULID_COMPLETION_TOKEN=completion-secret",
                "-e",
                "OPEN_TULID_MODEL_SESSION_TOKEN=model-secret",
                "-e",
                'OPEN_TULID_MODEL_ENDPOINTS=[{"token":"json-secret"}]',
                "-e",
                "OPENAI_API_KEY=provider-secret",
                "-e",
                "KEEP_ME=visible",
            ),
            returncode=1,
        ),
    )
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    executor.run(JOB_ID)

    command_log = (workspace / ".open-tulid" / "logs" / "command.txt").read_text(encoding="utf-8")
    assert "completion-secret" not in command_log
    assert "model-secret" not in command_log
    assert "json-secret" not in command_log
    assert "provider-secret" not in command_log
    assert "OPEN_TULID_COMPLETION_TOKEN=<redacted>" in command_log
    assert "KEEP_ME=visible" in command_log
    trace = json.loads((workspace / ".open-tulid" / "logs" / "agent-run.json").read_text(encoding="utf-8"))
    trace_text = json.dumps(trace)
    assert trace["status"] == "finished"
    assert trace["job"]["job_id"] == JOB_ID
    assert trace["agent"]["env"]["OPEN_TULID_COMPLETION_TOKEN"] == "<redacted>"
    assert trace["result"]["returncode"] == 1
    assert "completion-secret" not in trace_text
    assert "model-secret" not in trace_text
    assert "json-secret" not in trace_text
    assert "provider-secret" not in trace_text


def test_executor_marks_job_failed_when_internal_error_occurs_after_running(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True

    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        lambda request, *, docker_executable: (_ for _ in ()).throw(RuntimeError("docker exploded")),
    )
    events = JsonlEventStore(tmp_path / "events")
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=events,
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)
    loaded = store.get(JOB_ID)

    assert result.accepted is False
    assert loaded.job is not None
    assert loaded.job.status == "failed"
    assert loaded.job.metadata["failure_reason"] == "executor_exception"
    assert [event.event_type for event in events.iter_events()] == ["ExecutionStarted", "ExecutionFailed"]


def test_executor_marks_job_failed_when_workspace_preparation_fails(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    from open_tulid.runtime.workspaces import WorkspacePrepareResult
    monkeypatch.setattr(
        "open_tulid.runtime.executor.WorkspacePreparer.prepare",
        lambda self, **kwargs: WorkspacePrepareResult(error=DomainError(
            code="workspace.prepare_failed",
            message="cannot prepare",
        )),
    )
    events = JsonlEventStore(tmp_path / "events")
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=events,
        runtime=RuntimeConfig(completion_host="127.0.0.1", completion_container_host="127.0.0.1"),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
    )

    result = executor.run(JOB_ID)
    loaded = store.get(JOB_ID)

    assert result.accepted is False
    assert loaded.job is not None
    assert loaded.job.status == "failed"
    assert loaded.job.metadata["failure_reason"] == "workspace.prepare_failed"
    assert [event.event_type for event in events.iter_events()] == ["ExecutionFailed"]


def test_executor_passes_scoped_model_proxy_session_to_worker(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    leases = FileResourceLeaseStore(
        tmp_path / "leases",
        {"remote-llm": ResourceConfig(kind="model", capacity=1, proxy="openai")},
    )
    from open_tulid.runtime import ModelProxySessionStore
    sessions = ModelProxySessionStore()
    seen = {}

    def fake_run(request, *, docker_executable):
        seen.update(request.env)
        return AgentRunResult(agent_id=request.agent_id, image=request.image, command=("fake",), returncode=1)

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run)
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_resources={"codex": ("remote-llm",)},
            worker_model_env={
                "codex": {
                    "OPENAI_BASE_URL": "{endpoint}",
                    "OPENAI_API_KEY": "{token}",
                },
            },
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
        lease_store=leases,
        resources={"remote-llm": ResourceConfig(kind="model", capacity=1, proxy="openai")},
        model_proxy_sessions=sessions,
        model_proxy_endpoint_base="http://host.docker.internal:8787",
    )

    executor.run(JOB_ID)

    assert seen["OPEN_TULID_MODEL_ENDPOINT"] == "http://host.docker.internal:8787/proxies/openai"
    assert seen["OPEN_TULID_MODEL_PROXY_ID"] == "openai"
    assert seen["OPEN_TULID_MODEL_SESSION_TOKEN"]
    assert seen["OPENAI_BASE_URL"] == "http://host.docker.internal:8787/proxies/openai"
    assert seen["OPENAI_API_KEY"] == seen["OPEN_TULID_MODEL_SESSION_TOKEN"]
    assert sessions.get(seen["OPEN_TULID_MODEL_SESSION_TOKEN"]) is None


def test_executor_writes_opencode_config_for_tulid_model_proxy(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="qwen_27b",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    leases = FileResourceLeaseStore(
        tmp_path / "leases",
        {"qwen-local": ResourceConfig(kind="model", capacity=1, proxy="qwen")},
    )
    from open_tulid.runtime import ModelProxySessionStore
    sessions = ModelProxySessionStore()
    seen = {}

    def fake_run(request, *, docker_executable):
        seen["args"] = request.args
        seen["config"] = json.loads((Path(request.workspace) / "opencode.json").read_text(encoding="utf-8"))
        seen["env"] = request.env
        return AgentRunResult(agent_id=request.agent_id, image=request.image, command=("fake",), returncode=1)

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run)
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_args={
                "qwen_27b": (
                    "run",
                    "--model",
                    "tulid-qwen/Qwen3.6-27B-MTP-UD-Q6_K_XL.gguf",
                ),
            },
            worker_resources={"qwen_27b": ("qwen-local",)},
            worker_types={"qwen_27b": "opencode"},
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
        lease_store=leases,
        resources={"qwen-local": ResourceConfig(kind="model", capacity=1, proxy="qwen")},
        model_proxy_sessions=sessions,
        model_proxy_endpoint_base="http://host.docker.internal:8787",
    )

    executor.run(JOB_ID)

    assert seen["args"] == ("run", "--model", "tulid-qwen/Qwen3.6-27B-MTP-UD-Q6_K_XL.gguf")
    assert seen["config"]["model"] == "tulid-qwen/Qwen3.6-27B-MTP-UD-Q6_K_XL.gguf"
    assert seen["config"]["small_model"] == "tulid-qwen/Qwen3.6-27B-MTP-UD-Q6_K_XL.gguf"
    provider = seen["config"]["provider"]["tulid-qwen"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "http://host.docker.internal:8787/proxies/qwen",
        "apiKey": "{env:OPEN_TULID_MODEL_SESSION_TOKEN}",
    }
    assert "Qwen3.6-27B-MTP-UD-Q6_K_XL.gguf" in provider["models"]


def test_executor_passes_all_required_model_proxy_sessions(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    leases = FileResourceLeaseStore(
        tmp_path / "leases",
        {
            "remote-a": ResourceConfig(kind="model", capacity=1, proxy="openai-a"),
            "remote-b": ResourceConfig(kind="model", capacity=1, proxy="openai-b"),
        },
    )
    from open_tulid.runtime import ModelProxySessionStore
    sessions = ModelProxySessionStore()
    seen = {}
    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        lambda request, *, docker_executable: seen.update(request.env) or AgentRunResult(
            agent_id=request.agent_id, image=request.image, command=("fake",), returncode=1,
        ),
    )
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_resources={"codex": ("remote-a", "remote-b")},
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
        lease_store=leases,
        resources={
            "remote-a": ResourceConfig(kind="model", capacity=1, proxy="openai-a"),
            "remote-b": ResourceConfig(kind="model", capacity=1, proxy="openai-b"),
        },
        model_proxy_sessions=sessions,
        model_proxy_endpoint_base="http://host.docker.internal:8787",
    )

    executor.run(JOB_ID)

    endpoints = json.loads(seen["OPEN_TULID_MODEL_ENDPOINTS"])
    assert [item["proxy_id"] for item in endpoints] == ["openai-a", "openai-b"]
    assert "OPEN_TULID_MODEL_ENDPOINT" not in seen


def test_executor_mounts_subscription_auth_without_proxy_session(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    auth_home = tmp_path / ".codex"
    auth_home.mkdir()
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id=JOB_ID,
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret"},
    )).accepted is True
    from open_tulid.runtime import ModelProxySessionStore
    sessions = ModelProxySessionStore()
    seen = {}
    monkeypatch.setattr(
        "open_tulid.runtime.executor.run_agent_container",
        lambda request, *, docker_executable: seen.update({"env": request.env, "mounts": request.mounts})
        or AgentRunResult(agent_id=request.agent_id, image=request.image, command=("fake",), returncode=1),
    )
    executor = JobExecutor(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=JsonlEventStore(tmp_path / "events"),
        runtime=RuntimeConfig(
            completion_host="127.0.0.1",
            completion_container_host="127.0.0.1",
            worker_resources={"codex": ("codex-subscription",)},
        ),
        project_config=ProjectConfig(name="Agent", tracker_path="Agent"),
        resources={"codex-subscription": ResourceConfig(kind="model", capacity=4, proxy="chatgpt-codex")},
        model_proxies={
            "chatgpt-codex": ModelProxyConfig(
                kind="subscription",
                auth_home=auth_home,
                container_auth_home="/root/.codex",
            ),
        },
        model_proxy_sessions=sessions,
        model_proxy_endpoint_base="http://host.docker.internal:8787",
    )

    executor.run(JOB_ID)

    assert seen["mounts"] == (ContainerMount(auth_home, "/root/.codex"),)
    assert "OPEN_TULID_MODEL_ENDPOINT" not in seen["env"]


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "CodeReview": StateDefinition(id="CodeReview"),
        }),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex",
                requires=RequirementDefinition(artifacts=("result.md",)),
                transaction=None,
            ),
        }),
    )


def _workflow_without_requirements() -> WorkflowDefinition:
    workflow = _workflow()
    return WorkflowDefinition(
        schema_version=workflow.schema_version,
        states=workflow.states,
        task_types=workflow.task_types,
        artifact_types=workflow.artifact_types,
        validation_types=workflow.validation_types,
        operation_types=workflow.operation_types,
        workers=workflow.workers,
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code",
                task_type="task",
                from_state="Todo",
                to_state="CodeReview",
                worker="codex",
                requires=RequirementDefinition(),
                transaction=None,
            ),
        }),
    )


def _task() -> Task:
    return Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
        body="Make the thing work.",
    )
