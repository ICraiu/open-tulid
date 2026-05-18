from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.containers.runtime import AgentRunResult, ContainerMount
from open_tulid.domain import (
    DomainError,
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkerDefinition,
    WorkflowDefinition,
)
from open_tulid.models import ModelProxyConfig, ProjectConfig, ResourceConfig, RuntimeConfig
from open_tulid.runtime import FileExecutionJobStore, FileResourceLeaseStore, JobExecutor, JsonlEventStore


TASK_ID = "01J00000000000000000000001"
JOB_ID = "01J00000000000000000000JOB"


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
    assert adapter.moved_to == "CodeReview"
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "accepted"


def test_executor_injects_linked_context_and_instructions(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    (project / "artifacts").mkdir(parents=True)
    (project / "docs").mkdir()
    (project / "agents").mkdir()
    (project / "artifacts" / "spec.md").write_text("Spec sees [[extra]].\n", encoding="utf-8")
    (project / "docs" / "extra.md").write_text("Extra context.\n", encoding="utf-8")
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
            return ReadTaskResult(task=Task(
                id=TASK_ID,
                title="Task",
                path="tasks/task.md",
                current_state="Todo",
                task_type="task",
                artifact_links=("artifacts/spec.md",),
                body="Task body.",
            ))

    def fake_run_agent_container(request, *, docker_executable):
        output = Path(request.workspace) / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.md").write_text("done\n", encoding="utf-8")
        prompt = (Path(request.workspace) / ".open-tulid" / "prompt-packet.md").read_text(encoding="utf-8")
        assert "Task body." in prompt
        assert "Spec sees" in prompt
        assert "Extra context." in prompt
        assert "Default instructions." in prompt
        return AgentRunResult(agent_id=request.agent_id, image=request.image, command=("fake",), returncode=17)

    monkeypatch.setattr("open_tulid.runtime.executor.run_agent_container", fake_run_agent_container)
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


def _task() -> Task:
    return Task(
        id=TASK_ID,
        title="Implement thing",
        path="tasks/thing.md",
        current_state="Todo",
        task_type="task",
        body="Make the thing work.",
    )
