from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import open_tulid.adapters.obsidian as obsidian_module
from open_tulid.adapters import (
    ObsidianAdapter,
    ObsidianAdapterConfig,
    ObsidianStateMapping,
    config_from_workflow,
)
from open_tulid.domain import (
    ObsidianStateMappingDefinition,
    ObsidianStorageDefinition,
    ProjectSnapshot,
    StateDefinition,
    StorageDefinition,
    Task,
    WorkflowDefinition,
)


TASK_ID = "01J00000000000000000000001"


def _adapter(project_root: Path) -> ObsidianAdapter:
    return ObsidianAdapter(ObsidianAdapterConfig(
        project_id="Agent",
        project_root=project_root,
        boards={"Work": "kanban/Work.md"},
        state_mappings=(
            ObsidianStateMapping(state="Todo", board="Work", column="Todo"),
            ObsidianStateMapping(state="InProgress", board="Work", column="In progress"),
        ),
    ))


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "Agent"
    (project / "kanban").mkdir(parents=True)
    (project / "tasks").mkdir(parents=True)
    (project / "events").mkdir(parents=True)
    return project


def _workflow_with_obsidian_storage() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        states={"Todo": StateDefinition(id="Todo")},
        task_types={},
        artifact_types={},
        validation_types={},
        operation_types={},
        workers={},
        transitions={},
        storage=StorageDefinition(obsidian=ObsidianStorageDefinition(
            boards={"Work": "kanban/Work.md"},
            state_mappings=(
                ObsidianStateMappingDefinition(state="Todo", board="Work", column="Todo"),
            ),
        )),
    )


def test_builds_obsidian_adapter_config_from_compiled_workflow(tmp_path: Path):
    project = _make_project(tmp_path)

    config = config_from_workflow(
        project_id="Agent",
        project_root=project,
        workflow=_workflow_with_obsidian_storage(),
    )

    assert config.boards == {"Work": "kanban/Work.md"}
    assert config.state_mappings == (ObsidianStateMapping(state="Todo", board="Work", column="Todo"),)


def _write_task(project: Path, note: str, task_id: str = TASK_ID, state: str | None = None) -> None:
    state_line = f"state: {state}\n" if state else ""
    (project / "tasks" / f"{note}.md").write_text(
        "---\n"
        f"id: {task_id}\n"
        "type: task\n"
        f"{state_line}"
        "---\n"
        "\n"
        "# Add health-check endpoint\n"
        "\n"
        "## Task\n"
        "Add a /healthz endpoint.\n",
        encoding="utf-8",
    )


class TestObsidianAdapterLoadProject:
    def test_loads_board_cards_as_domain_snapshot(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz", state="Todo")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n"
            "\n"
            "## In progress\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is True
        assert isinstance(result.snapshot, ProjectSnapshot)
        assert result.snapshot is not None
        task = result.snapshot.tasks[TASK_ID]
        assert isinstance(task, Task)
        assert task.current_state == "Todo"
        assert task.title == "Add health-check endpoint"
        assert result.snapshot.board_positions[TASK_ID].column == "Todo"

    def test_rejects_duplicate_task_ids(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, "one", task_id=TASK_ID)
        _write_task(project, "two", task_id=TASK_ID)
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n- [ ] [[one]]\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is False
        assert [e.code for e in result.errors] == ["task.duplicate_id"]

    def test_rejects_duplicate_active_cards(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n"
            "\n"
            "## In progress\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is False
        assert [e.code for e in result.errors] == ["task.duplicate_active_card"]

    def test_rejects_frontmatter_state_mismatch(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz", state="InProgress")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is False
        assert [e.code for e in result.errors] == ["task.state_mismatch"]

    def test_rejects_task_without_active_card(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz")
        (project / "kanban" / "Work.md").write_text("## Todo\n", encoding="utf-8")

        result = _adapter(project).load_project()

        assert result.accepted is False
        assert [e.code for e in result.errors] == ["task.missing_active_card"]

    def test_rejects_malformed_board_row(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            "not a card\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is False
        assert [e.code for e in result.errors] == [
            "board.invalid_task_row",
            "task.missing_active_card",
        ]

    def test_rejects_malformed_task_frontmatter(self, tmp_path: Path):
        project = _make_project(tmp_path)
        (project / "tasks" / f"{TASK_ID}-add-healthz.md").write_text(
            "---\n"
            "id: [unterminated\n"
            "---\n",
            encoding="utf-8",
        )
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is False
        assert result.errors[0].code == "task.invalid_frontmatter"

    def test_assigns_missing_task_id_without_renaming_note_or_card(self, tmp_path: Path):
        project = _make_project(tmp_path)
        task_path = project / "tasks" / "Add healthz.md"
        task_path.write_text("# Add healthz\n\n## Task\nAdd endpoint.\n", encoding="utf-8")
        board_path = project / "kanban" / "Work.md"
        original_board = "## Todo\n- [ ] [[Add healthz]]\n"
        board_path.write_text(original_board, encoding="utf-8")

        result = _adapter(project).load_project()

        assert result.accepted is True
        assert result.snapshot is not None
        assert len(result.snapshot.tasks) == 1
        task_id = next(iter(result.snapshot.tasks))
        assert task_id == "1"
        assert task_path.exists()
        assert "id: '1'" in task_path.read_text(encoding="utf-8")
        assert board_path.read_text(encoding="utf-8") == original_board

    def test_loads_numeric_task_ids(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, "1-add-healthz", task_id="1", state="Todo")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            "- [ ] [[1-add-healthz]]\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is True
        assert result.snapshot is not None
        assert result.snapshot.tasks["1"].title == "Add health-check endpoint"


class TestObsidianAdapterEffects:
    def test_moves_task_by_id_and_preserves_card_text(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n"
            "\n"
            "## In progress\n",
            encoding="utf-8",
        )

        result = _adapter(project).move_task(TASK_ID, "InProgress")

        assert result.accepted is True
        assert (project / "kanban" / "Work.md").read_text(encoding="utf-8") == (
            "## Todo\n"
            "\n"
            "## In progress\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n"
        )

    def test_moves_task_and_updates_frontmatter_state_cache(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz", state="Todo")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n"
            "\n"
            "## In progress\n",
            encoding="utf-8",
        )

        result = _adapter(project).move_task(TASK_ID, "InProgress")
        load_result = _adapter(project).load_project()

        assert result.accepted is True
        assert load_result.accepted is True
        content = (project / "tasks" / f"{TASK_ID}-add-healthz.md").read_text(encoding="utf-8")
        assert "state: InProgress" in content

    def test_move_rolls_back_task_state_cache_when_board_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        project = _make_project(tmp_path)
        task_path = project / "tasks" / f"{TASK_ID}-add-healthz.md"
        _write_task(project, f"{TASK_ID}-add-healthz", state="Todo")
        original_task_content = task_path.read_text(encoding="utf-8")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n"
            "\n"
            "## In progress\n",
            encoding="utf-8",
        )
        original_replace = os.replace

        def fail_board_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            if Path(dst).name == "Work.md":
                raise OSError("injected board write failure")
            original_replace(src, dst)

        monkeypatch.setattr(obsidian_module.os, "replace", fail_board_replace)

        result = _adapter(project).move_task(TASK_ID, "InProgress")

        assert result.accepted is False
        assert task_path.read_text(encoding="utf-8") == original_task_content

    def test_writes_existing_task_from_domain_object(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n",
            encoding="utf-8",
        )
        read_result = _adapter(project).read_task(TASK_ID)
        assert read_result.task is not None
        task = Task(
            id=read_result.task.id,
            title="Renamed task",
            path=read_result.task.path,
            current_state="Todo",
            body="Updated body\n",
        )

        result = _adapter(project).write_task(task)

        assert result.accepted is True
        content = (project / "tasks" / f"{TASK_ID}-add-healthz.md").read_text(encoding="utf-8")
        assert "id: 01J00000000000000000000001" in content
        assert "state: Todo" in content
        assert "# Renamed task" in content
        assert "Updated body" in content

    def test_appends_jsonl_event(self, tmp_path: Path):
        project = _make_project(tmp_path)
        (project / "kanban" / "Work.md").write_text("## Todo\n", encoding="utf-8")
        event = {
            "timestamp": "2026-05-09T12:00:00Z",
            "event_type": "TaskMoved",
            "task_id": TASK_ID,
        }

        result = _adapter(project).append_event(event)

        assert result.accepted is True
        event_path = project / "events" / "2026-05-09.jsonl"
        assert json.loads(event_path.read_text(encoding="utf-8")) == event

    def test_creates_new_task_and_places_board_card(self, tmp_path: Path):
        project = _make_project(tmp_path)
        (project / "kanban" / "Work.md").write_text("## Todo\n", encoding="utf-8")
        task = Task(
            id="2",
            title="Derived child",
            path="tasks/unused.md",
            current_state="Todo",
            task_type="task",
            parent_id="1",
            body="Child body\n",
        )

        result = _adapter(project).create_task(task)

        assert result.accepted is True
        assert (project / "tasks" / "2-derived-child.md").is_file()
        assert "[[2-derived-child]]" in (project / "kanban" / "Work.md").read_text(encoding="utf-8")
