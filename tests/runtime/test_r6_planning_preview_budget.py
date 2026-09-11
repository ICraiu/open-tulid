from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from open_tulid.adapters.base import LoadProjectResult, ReadTaskResult
from open_tulid.domain import (
    DerivesDefinition,
    ProjectSnapshot,
    RequirementDefinition,
    TransitionDefinition,
    Task,
    WorkflowDefinition,
)
from open_tulid.runtime.executor import render_execution_prompt
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.planning_inputs import (
    freeze_planning_inputs,
    frozen_repository_baseline_from_snapshot,
    load_planning_inputs,
)
from open_tulid.runtime.prompts import PREVIEW_JOB_ID, normalize_ephemeral_completion_fields
from open_tulid.runtime.repository_facts import capture_repository_snapshot


TASK_ID = "01J00000000000000000000001"


class FakeAdapter:
    def __init__(self, root: Path, current: Task, existing: Task):
        self.root = root
        self._tasks = {current.id: current, existing.id: existing}
        self.config = type("Cfg", (), {"project_root": root})()

    def load_project(self):
        return LoadProjectResult(snapshot=ProjectSnapshot(
            project_id="Agent", tasks=MappingProxyType(self._tasks), board_positions=MappingProxyType({}),
        ))

    def read_task(self, task_id: str):
        task = self._tasks.get(task_id)
        return ReadTaskResult(task=task) if task is not None else ReadTaskResult()


def _breakdown_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({}),
        task_types=MappingProxyType({}),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            "BreakDownImplementationSpec": TransitionDefinition(
                id="BreakDownImplementationSpec",
                task_type="QuestionRound",
                from_state="ReadyForBreakdown",
                to_state="Done",
                worker="codex_breakdown",
                requires=RequirementDefinition(),
                transaction=None,
                derives=DerivesDefinition(
                    task_type="ImplementationTask", state="Todo", artifact_type="ImplementationTaskFile",
                ),
            ),
        }),
    )


def _setup(tmp_path: Path, *, add_repo: bool = True):
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "package.json").write_text('{"name":"app","scripts":{"test":"jest"}}\n', encoding="utf-8")
    (project / "tasks").mkdir(exist_ok=True)
    (project / "tasks" / "existing.md").write_text("Existing unfinished task body.\n", encoding="utf-8")
    current = Task(id=TASK_ID, title="Plan", path="tasks/plan.md", current_state="ReadyForBreakdown",
                   task_type="QuestionRound", body="Break down the work.")
    existing = Task(id="01J00000000000000000000002", title="Existing", path="tasks/existing.md",
                    current_state="Todo", task_type="ImplementationTask", body="Existing unfinished task body.")
    adapter = FakeAdapter(project, current, existing)
    repo = None
    snapshot = None
    if add_repo:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "main.js").write_text("console.log('x')\n", encoding="utf-8")
        snapshot = capture_repository_snapshot(repo)
    workflow = _breakdown_workflow()
    transition = workflow.transitions["BreakDownImplementationSpec"]
    return adapter, workflow, transition, current, repo, snapshot


def _render(workflow, adapter, current, transition, *, job_id: str, repository_facts=None):
    return render_execution_prompt(
        workflow=workflow, adapter=adapter, task=current, transition=transition,
        worker_id="codex_breakdown", job_id=job_id,
        completion_endpoint=f"http://preview.invalid/jobs/{job_id}/complete",
        project_root=adapter.root, repository_facts=repository_facts,
    )


def test_planning_preview_matches_scheduled_packet_after_ephemeral_normalization(tmp_path: Path):
    adapter, workflow, transition, current, repo, snapshot = _setup(tmp_path)
    preview = _render(workflow, adapter, current, transition, job_id=PREVIEW_JOB_ID, repository_facts=snapshot)

    from open_tulid.domain import ExecutionJob
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB", project_id="Agent", task_id=current.id,
        transition_id=transition.id, worker_id="codex_breakdown",
        workspace_path=str(tmp_path / "w"),
        metadata={"planning_inputs": freeze_planning_inputs(
            current, transition, preview.text, preview.context_files,
            repository_baseline=frozen_repository_baseline_from_snapshot(snapshot),
        )},
    )).accepted
    job = store.get("01J00000000000000000000JOB").job
    scheduled = load_planning_inputs(job)
    assert scheduled is not None

    assert normalize_ephemeral_completion_fields(preview.text, job_id=PREVIEW_JOB_ID) == \
           normalize_ephemeral_completion_fields(scheduled.prompt, job_id=PREVIEW_JOB_ID)


def test_planning_inline_budget_overflow_is_named_error_no_truncation(tmp_path, monkeypatch):
    adapter, workflow, transition, current, repo, snapshot = _setup(tmp_path)
    import open_tulid.runtime.executor as executor_module
    monkeypatch.setattr(executor_module, "INLINE_CHARACTER_LIMIT", 300)
    # A long task body plus planning inputs must exceed the tiny budget.
    oversized = type(current)(
        id=current.id, title=current.title, path=current.path, current_state=current.current_state,
        task_type=current.task_type, parent_id=current.parent_id, metadata=current.metadata,
        dependencies=current.dependencies, artifact_links=current.artifact_links,
        body="Plan exactly this behavior " + "over and over " * 60,
    )
    rendered = _render(workflow, adapter, oversized, transition, job_id=PREVIEW_JOB_ID, repository_facts=snapshot)
    assert rendered.accepted is False
    assert any(error.code == "prompt.budget_exceeded" for error in rendered.errors)
