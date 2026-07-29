from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from open_tulid.domain import (
    ExecutionJob,
    RequirementDefinition,
    Task,
    TransitionDefinition,
    ValidationCallDefinition,
)
from open_tulid.runtime.execution_contracts import (
    compile_task_execution_contract,
    execution_contract_to_dict,
    load_job_execution_contract,
)
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.task_contracts import task_source_intent_sha256
from open_tulid.runtime.workspaces import WorkspacePreparer


TASK_ID = "task-1"


def _task_and_contract(project_root: Path) -> Task:
    task = Task(
        id=TASK_ID,
        title="Add health",
        path="tasks/task-1.md",
        current_state="ReadyToImplement",
        task_type="ImplementationTask",
        metadata={"priority": "high"},
        body="Add a deterministic health endpoint.",
    )
    relative = Path(
        "artifacts/task-1/ImplementationContract/implementation-contract.yaml"
    )
    path = project_root / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""\
schema: tulid.implementation/v1
source:
  task_id: "{task.id}"
  source_intent_sha256: "{task_source_intent_sha256(task)}"
profile: code_change
objective: Add a deterministic health endpoint.
change_surface:
  add: []
  edit: [app.py]
  forbidden: [secrets/]
interfaces:
  - name: app.healthz
    behavior: Return ok.
requirements:
  - Preserve existing behavior.
failure_behavior: []
non_goals: []
checks:
  focused:
    - id: tests_pass
      argv: [python, check_repo.py, tests]
      timeout_seconds: 90
      expect:
        exit_code: 0
  invariants: [project_build]
""",
        encoding="utf-8",
    )
    return replace(task, artifact_links=(relative.as_posix(),))


def _transition(*, tests_command: str = "python check_repo.py tests"):
    return TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="ReadyToImplement",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(
            validations=(
                ValidationCallDefinition(
                    type="tests_pass",
                    args=MappingProxyType({"command": tests_command}),
                ),
                ValidationCallDefinition(
                    type="project_build",
                    args=MappingProxyType({
                        "command": ["python", "check_repo.py", "build"],
                    }),
                ),
            ),
            changed_files_required=True,
        ),
        transaction=None,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("app.py").write_text(
        "def healthz():\n    return 'pending'\n",
        encoding="utf-8",
    )
    repo.joinpath("check_repo.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    return repo


def test_compile_freezes_task_transition_repository_and_resolved_checks(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    repo = _repo(tmp_path)

    first = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    second = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )

    assert first.accepted is True
    assert first.contract is not None
    assert second.contract is not None
    assert first.contract.sha256 == second.contract.sha256
    assert first.contract.source_task == task
    assert first.contract.repository_facts.repository_available is True
    assert [entry.path for entry in first.contract.baseline_manifest.entries] == [
        "app.py",
        "check_repo.py",
    ]
    assert [check.id for check in first.contract.resolved_checks] == [
        "project_build",
        "tests_pass",
    ]
    tests_check = first.contract.resolved_checks[1]
    assert tests_check.source == "task+transition"
    assert tests_check.argv == ("python", "check_repo.py", "tests")
    assert tests_check.timeout_seconds == 90


def test_compile_rejects_conflicting_task_and_transition_commands(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)

    result = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(tests_command="python check_repo.py all"),
    )

    assert result.accepted is False
    assert result.errors[0].code == "execution_contract.check_conflict"


def test_job_contract_round_trips_and_detects_tampering(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled.contract is not None
    payload = execution_contract_to_dict(compiled.contract)
    job = ExecutionJob(
        job_id="job-1",
        project_id="Agent",
        task_id=task.id,
        transition_id="ImplementTask",
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": payload,
            "execution_contract_sha256": compiled.contract.sha256,
        },
    )

    loaded = load_job_execution_contract(job, required=True)

    assert loaded.accepted is True
    assert loaded.contract is not None
    assert loaded.contract.source_task == task
    assert loaded.contract.transition == _transition()

    payload["source"]["task"]["body"] = "Silently broadened task."
    tampered = load_job_execution_contract(job, required=True)
    assert tampered.accepted is False
    assert tampered.errors[0].code == "execution_contract.hash_mismatch"


def test_job_store_rejects_frozen_contract_replacement(tmp_path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    created = store.create(ExecutionJob(
        job_id="job-1",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="ImplementTask",
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": {"schema": "original"},
            "execution_contract_sha256": "a" * 64,
        },
    ))
    assert created.accepted is True

    updated = store.update_status(
        "job-1",
        "running",
        metadata={"execution_contract_sha256": "b" * 64},
    )

    assert updated.accepted is False
    assert updated.error is not None
    assert updated.error.code == "job.immutable_metadata"


def test_workspace_writes_frozen_contract_files_and_rejects_repo_drift(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    repo = _repo(tmp_path)
    transition = _transition()
    compiled = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=transition,
    )
    assert compiled.contract is not None
    job = ExecutionJob(
        job_id="job-1",
        project_id="Agent",
        task_id=task.id,
        transition_id=transition.id,
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": execution_contract_to_dict(compiled.contract),
            "execution_contract_sha256": compiled.contract.sha256,
        },
    )

    prepared = WorkspacePreparer(repo_root=repo).prepare(
        job=job,
        task=task,
        transition=transition,
    )

    assert prepared.accepted is True
    context_root = Path(job.workspace_path) / ".open-tulid"
    assert context_root.joinpath("execution-contract.json").is_file()
    assert context_root.joinpath("repository-facts.json").is_file()
    assert context_root.joinpath("baseline-manifest.json").is_file()

    drifted_workspace = tmp_path / "drifted-workspace"
    drifted_job = replace(job, workspace_path=str(drifted_workspace))
    repo.joinpath("app.py").write_text("changed after scheduling\n", encoding="utf-8")

    drifted = WorkspacePreparer(repo_root=repo).prepare(
        job=drifted_job,
        task=task,
        transition=transition,
    )

    assert drifted.accepted is False
    assert drifted.error is not None
    assert drifted.error.code == "workspace.baseline_mismatch"
