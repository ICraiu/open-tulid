from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from open_tulid.runtime.acceptance_profiles import load_acceptance_profiles
from open_tulid.runtime.execution_contracts import compile_task_execution_contract
from open_tulid.runtime.task_contracts import task_source_intent_sha256
from open_tulid.domain import RequirementDefinition, Task, TransitionDefinition


def _task(project: Path) -> Task:
    task = Task(id="task-1", title="Health", path="tasks/task-1.md", current_state="Ready", task_type="ImplementationTask", body="Add health.")
    artifact = project / "artifacts/task-1/ImplementationContract/contract.yaml"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(f"""schema: tulid.implementation/v1
source:
  task_id: task-1
  source_intent_sha256: {task_source_intent_sha256(task)}
profile: code_change
objective: Add health.
change_surface:
  add: []
  edit: [app.py]
  forbidden: []
requirements: [Health works.]
failure_behavior: []
non_goals: []
checks:
  focused: []
  invariants: []
  profiles: [unit, vertical]
""", encoding="utf-8")
    return replace(task, artifact_links=(artifact.relative_to(project).as_posix(),))


def test_profiles_are_loaded_and_frozen_as_resolved_checks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project.joinpath("acceptance.yaml").write_text("""schema: tulid.acceptance/v1
profiles:
  unit:
    kind: unit
    argv: [python, check.py, unit]
  vertical:
    kind: vertical_slice
    argv: [python, check.py, vertical]
    timeout_seconds: 90
    expect: {exit_code: 0, stdout_contains: [ok]}
""", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("app.py").write_text("pass\n", encoding="utf-8")
    repo.joinpath("check.py").write_text("print('ok')\n", encoding="utf-8")
    task = _task(project)
    transition = TransitionDefinition(id="Implement", task_type="ImplementationTask", from_state="Ready", to_state="Review", worker="qwen", requires=RequirementDefinition(), transaction=None)

    result = compile_task_execution_contract(project_root=project, repo_root=repo, task=task, transition=transition)

    assert result.accepted is True
    assert result.contract is not None
    assert [(item.id, item.source, item.timeout_seconds) for item in result.contract.resolved_checks] == [
        ("unit", "acceptance_profile", 120), ("vertical", "acceptance_profile", 90),
    ]


def test_unknown_or_unsafe_profile_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project.joinpath("acceptance.yaml").write_text("""schema: tulid.acceptance/v1
profiles:
  bad:
    kind: unit
    argv: [python, check.py, '&&']
""", encoding="utf-8")

    loaded = load_acceptance_profiles(project)

    assert loaded.accepted is False
    assert loaded.errors[0].code == "acceptance_profiles.profile_invalid"
