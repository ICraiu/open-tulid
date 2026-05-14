from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.containers.runtime import AgentRunResult
from open_tulid.domain import (
    ExecutionJob,
    ProjectSnapshot,
    RequirementDefinition,
    StateDefinition,
    Task,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkflowDefinition,
)
from open_tulid.models import ProjectConfig, RuntimeConfig
from open_tulid.runtime import FileExecutionJobStore, JobExecutor, JsonlEventStore


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
    assert seen["args"] == ("exec", "/workspace/project/.open-tulid/prompt-packet.md")
    assert str(seen["endpoint"]).startswith("http://127.0.0.1:")
    assert seen["prompt"] == "/workspace/project/.open-tulid/prompt-packet.md"
    assert (workspace / ".open-tulid" / "prompt-packet.md").is_file()
    assert adapter.moved_to == "CodeReview"
    loaded = store.get(JOB_ID)
    assert loaded.job is not None
    assert loaded.job.status == "accepted"


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
