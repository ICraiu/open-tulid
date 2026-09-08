from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

from open_tulid.domain import (
    ExecutionJob,
    RequirementDefinition,
    Task,
    TransitionDefinition,
    ValidationCallDefinition,
)
from open_tulid.runtime import WorkspacePreparer
from open_tulid.runtime.task_contracts import task_source_intent_sha256


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
        task=(task := Task(
            id="01J00000000000000000000001",
            title="Task",
            path="tasks/task.md",
            current_state="Todo",
            task_type="ImplementationTask",
            artifact_links=("docs/spec.md",),
            parent_id="parent-1",
            metadata={"priority": "high"},
            body="Free-form body.",
        )),
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
    assert f'"source_intent_sha256": "{task_source_intent_sha256(task)}"' in context
    assert '"artifact_links": [' in context
    assert '"parent_id": "parent-1"' in context
    assert '"priority": "high"' in context


_GLOBAL_CONTRACT = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: tests
    argv: [python, check_verify.py, tests]
    working_directory: .
    timeout_seconds: 300
    expect:
      exit_code: 0
"""


def _implementation_task(spec_relative: str):
    return Task(
        id="task-impl",
        title="Add health",
        path="tasks/task-impl.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Add a deterministic health endpoint.",
        artifact_links=(spec_relative,),
    )


def _implementation_transition():
    return TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="Todo",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(
            changed_files_required=True,
            validations=(
                ValidationCallDefinition(
                    type="tests_pass",
                    args=MappingProxyType({"command": "python check_verify.py tests"}),
                ),
            ),
        ),
        transaction=None,
    )


def _compile_global_contract(tmp_path: Path) -> tuple[Path, Task, TransitionDefinition, object]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "contract.yaml").write_text(_GLOBAL_CONTRACT, encoding="utf-8")
    spec = project_root / "docs" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec_text = "# Specification\n\nThe required enum is `HealthStatus`.\n"
    spec.write_text(spec_text, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "check_verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    task = _implementation_task("docs/spec.md")
    transition = _implementation_transition()
    from open_tulid.runtime.execution_contracts import compile_standard_execution_contract

    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=transition,
    )
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    assert compiled.contract is not None
    assert compiled.contract.context_files
    return project_root, task, transition, compiled.contract


def test_workspace_materializes_frozen_context_files_and_manifest(tmp_path: Path):
    from open_tulid.runtime.execution_contracts import execution_contract_to_dict

    project_root, task, transition, contract = _compile_global_contract(tmp_path)
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    frozen = contract.context_files[0]
    job = ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=task.id,
        transition_id=transition.id,
        worker_id="qwen",
        workspace_path=str(workspace),
        metadata={
            "execution_contract": execution_contract_to_dict(contract),
            "execution_contract_sha256": contract.sha256,
        },
    )

    result = WorkspacePreparer(repo_root=repo).prepare(
        job=job,
        task=task,
        transition=transition,
    )

    assert result.accepted is True, result.error
    context_root = workspace / ".open-tulid" / "context"
    target = context_root / frozen.workspace_path.split("/", 1)[1]
    assert target.read_text(encoding="utf-8") == frozen.content
    assert (project_root / "docs" / "spec.md").read_text(encoding="utf-8") == frozen.content
    manifest = json.loads((context_root / "context-files.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "tulid.context-files/v1"
    assert manifest["files"][0]["workspace_path"] == frozen.workspace_path
    assert manifest["files"][0]["sha256"] == frozen.sha256
    assert manifest["files"][0]["byte_count"] == frozen.byte_count
    assert manifest["files"][0]["refs"] == list(frozen.refs)
