from __future__ import annotations

from pathlib import Path

from open_tulid.adapters.base import LoadProjectResult
from open_tulid.domain import (
    DerivesDefinition,
    ProjectSnapshot,
    RequirementDefinition,
    Task,
    TransitionDefinition,
)
from open_tulid.runtime.planning_input import (
    build_planning_inputs,
    requires_planning_inputs,
)
from open_tulid.runtime.repository_facts import RepositorySnapshotResult, capture_repository_snapshot


def _task(*, task_id: str, title: str = "Task", current_state: str = "Todo", body: str = "", **kwargs) -> Task:
    return Task(
        id=task_id,
        title=title,
        path=f"tasks/{task_id}.md",
        current_state=current_state,
        body=body,
        **kwargs,
    )


def _project(*tasks: Task) -> LoadProjectResult:
    return LoadProjectResult(snapshot=ProjectSnapshot(
        project_id="Agent",
        tasks={task.id: task for task in tasks},
        board_positions={},
    ))


def _repository(repo_root: Path | None) -> RepositorySnapshotResult:
    return capture_repository_snapshot(repo_root)


def test_requires_planning_inputs_only_for_spec_and_breakdown():
    spec = TransitionDefinition(
        id="WriteImplementationSpec",
        task_type="QuestionRound",
        from_state="ReadyForSpec",
        to_state="ReadyForBreakdown",
        worker="codex_spec",
        requires=RequirementDefinition(artifacts=("ImplementationSpec",)),
        transaction=None,
    )
    breakdown = TransitionDefinition(
        id="BreakDownImplementationSpec",
        task_type="QuestionRound",
        from_state="ReadyForBreakdown",
        to_state="Done",
        worker="codex_breakdown",
        requires=RequirementDefinition(),
        transaction=None,
        derives=DerivesDefinition(task_type="ImplementationTask", state="Todo", artifact_type="ImplementationTaskFile"),
    )
    other = TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="Todo",
        to_state="SelfReview",
        worker="peon-llm",
        requires=RequirementDefinition(changed_files_required=True),
        transaction=None,
    )

    assert requires_planning_inputs(spec) is True
    assert requires_planning_inputs(breakdown) is True
    assert requires_planning_inputs(other) is False


def test_planning_inputs_include_repository_facts(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name":"app","scripts":{"test":"jest"}}\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text("console.log('hi')\n", encoding="utf-8")

    current = _task(task_id="01J00000000000000000000001")
    inputs = build_planning_inputs(
        repository=_repository(tmp_path),
        project=_project(current),
        current_task=current,
    )

    assert "# Planning Inputs" in inputs.text
    assert "## Repository Facts" in inputs.text
    assert "Repository is available." in inputs.text
    assert "package.json" in inputs.text
    assert "package.json#scripts.test" in inputs.text


def test_planning_inputs_list_unfinished_tasks_with_ids_states_dependencies(tmp_path: Path):
    current = _task(task_id="01J00000000000000000000001", body="Current task body.")
    unfinished = _task(
        task_id="01J00000000000000000000002",
        title="Existing integration",
        current_state="Todo",
        body="Body of existing unfinished work.",
        dependencies=("01J00000000000000000000001",),
    )
    done = _task(task_id="01J00000000000000000000003", title="Done work", current_state="Done")

    inputs = build_planning_inputs(
        repository=_repository(None),
        project=_project(current, unfinished, done),
        current_task=current,
    )

    assert "## Unfinished Project Tasks" in inputs.text
    assert "01J00000000000000000000002 — Existing integration" in inputs.text
    assert "State: Todo" in inputs.text
    assert "Dependencies: 01J00000000000000000000001" in inputs.text
    assert "Body of existing unfinished work." in inputs.text
    assert "01J00000000000000000000003" not in inputs.text


def test_planning_inputs_exclude_current_task_and_parent_lineage(tmp_path: Path):
    parent = _task(task_id="01J00000000000000000000009", title="Parent", current_state="Done")
    current = _task(task_id="01J00000000000000000000001")
    sibling = _task(task_id="01J00000000000000000000002", title="Sibling unfinished", current_state="ReadyToImplement")

    inputs = build_planning_inputs(
        repository=_repository(None),
        project=_project(current, parent, sibling),
        current_task=current,
        parent_tasks=(parent,),
    )

    assert "01J00000000000000000000001" not in inputs.text
    assert "01J00000000000000000000009" not in inputs.text
    assert "Sibling unfinished" in inputs.text


def test_planning_inputs_deduplicate_sources_and_do_not_import_linked_files(tmp_path: Path):
    current = _task(task_id="TASK", body="See [[background]].")
    unfinished = _task(
        task_id="TASK2",
        title="Unfinished",
        body="This unfinished task links [[imported]] but its linked files are not imported.",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "imported.md").write_text("Imported body.\n", encoding="utf-8")

    inputs = build_planning_inputs(
        repository=_repository(tmp_path),
        project=_project(current, unfinished),
        current_task=current,
    )

    assert inputs.text.count("### TASK2 — Unfinished") == 1
    assert "imported.md" not in inputs.text
    assert "Imported body." not in inputs.text


def test_planning_inputs_empty_without_repository_or_unfinished_tasks(tmp_path: Path):
    current = _task(task_id="01J00000000000000000000001")
    inputs = build_planning_inputs(
        repository=None,
        project=_project(current),
        current_task=current,
    )

    assert inputs.text == ""
