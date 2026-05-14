from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from open_tulid.domain import ExecutionJob, RequirementDefinition, Task, TransitionDefinition
from open_tulid.runtime import WorkspacePreparer


def test_workspace_preparer_copies_repo_and_writes_job_context(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("ignored\n", encoding="utf-8")
    workspace = tmp_path / "workspace"

    result = WorkspacePreparer(repo_root=repo).prepare(
        job=ExecutionJob(
            job_id="01J00000000000000000000JOB",
            project_id="Agent",
            task_id="01J00000000000000000000001",
            transition_id="code",
            worker_id="codex",
            workspace_path=str(workspace),
        ),
        task=Task(
            id="01J00000000000000000000001",
            title="Task",
            path="tasks/task.md",
            current_state="Todo",
        ),
        transition=TransitionDefinition(
            id="code",
            task_type="task",
            from_state="Todo",
            to_state="CodeReview",
            worker="codex",
            requires=RequirementDefinition(artifacts=("result.md",)),
            transaction=None,
        ),
        completion_endpoint="/jobs/01J00000000000000000000JOB/complete",
    )

    assert result.accepted is True
    assert (workspace / "README.md").is_file()
    assert not (workspace / ".git").exists()
    context = (workspace / ".open-tulid" / "job-context.json").read_text(encoding="utf-8")
    assert '"job_id": "01J00000000000000000000JOB"' in context
    assert '"required_artifacts": [' in context
