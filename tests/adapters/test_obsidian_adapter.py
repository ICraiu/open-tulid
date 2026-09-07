from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import open_tulid.adapters.obsidian as obsidian_module
from open_tulid.domain import (
    ProjectSnapshot,
    StateDefinition,
    StorageDefinition,
    Task,
    WorkflowDefinition,
)
from open_tulid.adapters.obsidian import (
    ObsidianAdapter,
    ObsidianAdapterConfig,
    ObsidianStateMapping,
    config_from_workflow,
)


TASK_ID = "01J00000000000000000000001"

_SCHEMA_TASK = (
    "# Add healthz\n"
    "\n"
    "Add a health check.\n"
    "\n"
    "## Why\n"
    "Why the check matters.\n"
    "\n"
    "## What\n"
    "What the check does.\n"
    "\n"
    "## How\n"
    "Which seam to touch.\n"
    "\n"
    "## Acceptance\n"
    "- the check is added\n"
)


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
        storage=StorageDefinition(
            config={
                "boards": {"Work": "kanban/Work.md"},
                "state_mappings": (
                    {"state": "Todo", "board": "Work", "column": "Todo"},
                ),
            },
        ),
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
        "Add a /healthz endpoint.\n"
        "\n"
        "## Why\n"
        "Surface service liveness.\n"
        "\n"
        "## What\n"
        "Add the endpoint.\n"
        "\n"
        "## How\n"
        "Touch the router seam.\n"
        "\n"
        "## Acceptance\n"
        "- the endpoint is added\n",
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

    def test_snapshot_uses_board_state_without_rewriting_frontmatter(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz", state="InProgress")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n"
            "\n"
            "## In progress\n",
            encoding="utf-8",
        )

        result = _adapter(project).load_project()

        assert result.accepted is True
        assert result.snapshot is not None
        assert result.snapshot.tasks[TASK_ID].current_state == "Todo"
        content = (project / "tasks" / f"{TASK_ID}-add-healthz.md").read_text(encoding="utf-8")
        assert "state: InProgress" in content

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

    def test_repair_assigns_missing_task_id_type_and_state_without_renaming_note_or_card(self, tmp_path: Path):
        project = _make_project(tmp_path)
        task_path = project / "tasks" / "Add healthz.md"
        task_path.write_text(_SCHEMA_TASK, encoding="utf-8")
        board_path = project / "kanban" / "Work.md"
        original_board = "## Todo\n- [ ] [[Add healthz]]\n"
        board_path.write_text(original_board, encoding="utf-8")

        result = _adapter(project).repair_project(fix=True)
        load_result = _adapter(project).load_project()

        assert result == ()
        assert load_result.accepted is True
        assert load_result.snapshot is not None
        assert len(load_result.snapshot.tasks) == 1
        task_id = next(iter(load_result.snapshot.tasks))
        assert task_id == "1"
        assert task_path.exists()
        content = task_path.read_text(encoding="utf-8")
        assert "id: '1'" in content
        assert "type: ProductIdea" in content
        assert "state: Todo" in content
        assert board_path.read_text(encoding="utf-8") == original_board

    def test_repair_reports_missing_task_metadata_without_fix(self, tmp_path: Path):
        project = _make_project(tmp_path)
        (project / "tasks" / "Add healthz.md").write_text(_SCHEMA_TASK.strip(), encoding="utf-8")
        (project / "kanban" / "Work.md").write_text("## Todo\n- [ ] [[Add healthz]]\n", encoding="utf-8")

        result = _adapter(project).repair_project(fix=False)

        assert [error.code for error in result] == [
            "task.id_missing",
            "task.type_missing",
            "task.state_mismatch",
        ]

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

    def test_move_repairs_stale_frontmatter_when_card_already_in_target_column(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz", state="Todo")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            "\n"
            "## In progress\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n",
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

    def test_write_task_does_not_duplicate_existing_title_heading(self, tmp_path: Path):
        project = _make_project(tmp_path)
        _write_task(project, f"{TASK_ID}-add-healthz", state="Todo")
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n",
            encoding="utf-8",
        )
        read_result = _adapter(project).read_task(TASK_ID)
        assert read_result.task is not None

        result = _adapter(project).write_task(read_result.task)

        assert result.accepted is True
        content = (project / "tasks" / f"{TASK_ID}-add-healthz.md").read_text(encoding="utf-8")
        assert content.count("# Add health-check endpoint") == 1

    def test_write_task_does_not_duplicate_title_after_leading_blank_body(self, tmp_path: Path):
        project = _make_project(tmp_path)
        (project / "tasks" / f"{TASK_ID}-add-healthz.md").write_text(
            "---\n"
            f"id: {TASK_ID}\n"
            "type: task\n"
            "state: Todo\n"
            "---\n"
            "\n"
            "\n"
            "# Add health-check endpoint\n"
            "\n"
            "Add a /healthz endpoint.\n",
            encoding="utf-8",
        )
        (project / "kanban" / "Work.md").write_text(
            "## Todo\n"
            f"- [ ] [[{TASK_ID}-add-healthz]]\n",
            encoding="utf-8",
        )
        read_result = _adapter(project).read_task(TASK_ID)
        assert read_result.task is not None

        result = _adapter(project).write_task(read_result.task)

        assert result.accepted is True
        content = (project / "tasks" / f"{TASK_ID}-add-healthz.md").read_text(encoding="utf-8")
        assert content.count("# Add health-check endpoint") == 1

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


_SCHEMA_BODY = (
    "Add a health check.\n"
    "\n"
    "## Why\n"
    "Why the check matters.\n"
    "\n"
    "## What\n"
    "What the check does.\n"
    "\n"
    "## How\n"
    "Which seam to touch.\n"
    "\n"
    "## Acceptance\n"
    "- the check is added\n"
)


def _repair_errors(
    tmp_path: Path,
    body: str,
    *,
    task_id: str = "1",
    declared_profile: str | None = None,
) -> list[str]:
    project = _make_project(tmp_path)
    (project / "kanban" / "Work.md").write_text("## Todo\n", encoding="utf-8")
    if declared_profile is not None:
        (project / "acceptance.yaml").write_text(
            "schema: tulid.acceptance/v1\n"
            "policy:\n"
            "  require_vertical_slice: false\n"
            "profiles:\n"
            f"  {declared_profile}:\n"
            "    kind: unit\n"
            "    argv: [pytest]\n",
            encoding="utf-8",
        )
    (project / "tasks" / "schematic.md").write_text(
        "---\n"
        f"id: {task_id}\n"
        "type: task\n"
        "state: Todo\n"
        "---\n"
        "\n"
        f"# Add healthz\n"
        "\n"
        f"{body}",
        encoding="utf-8",
    )
    return [error.code for error in _adapter(project).repair_project(fix=False)]


class TestTaskFileSchema:
    def test_five_part_task_file_validates(self, tmp_path: Path):
        assert _repair_errors(tmp_path, _SCHEMA_BODY) == []

    def test_missing_section_reports_section_missing(self, tmp_path: Path):
        body = _SCHEMA_BODY.replace("## Why\nWhy the check matters.\n", "")

        errors = _repair_errors(tmp_path, body)

        assert "task.section_missing" in errors

    def test_empty_section_reports_section_empty(self, tmp_path: Path):
        body = _SCHEMA_BODY.replace("## Why\nWhy the check matters.\n", "## Why\n")

        errors = _repair_errors(tmp_path, body)

        assert "task.section_empty" in errors

    def test_missing_description_reports_section_missing(self, tmp_path: Path):
        body = _SCHEMA_BODY.replace("Add a health check.\n", "")

        errors = _repair_errors(tmp_path, body)

        assert "task.section_missing" in errors

    def test_empty_acceptance_reports_criteria_missing(self, tmp_path: Path):
        body = _SCHEMA_BODY.replace("- the check is added\n", "")

        errors = _repair_errors(tmp_path, body)

        assert "task.acceptance_criteria_missing" in errors

    def test_undeclared_accepts_reports_run_unknown(self, tmp_path: Path):
        body = _SCHEMA_BODY.replace(
            "## Acceptance\n- the check is added\n",
            "## Acceptance\naccepts: [undeclared]\n",
        )

        errors = _repair_errors(tmp_path, body)

        assert "task.acceptance_run_unknown" in errors

    def test_declared_accepts_resolves(self, tmp_path: Path):
        body = _SCHEMA_BODY.replace(
            "## Acceptance\n- the check is added\n",
            "## Acceptance\naccepts: [unit]\naccepts_if:\n  - returns ok\n",
        )

        errors = _repair_errors(tmp_path, body, declared_profile="unit")

        assert "task.acceptance_run_unknown" not in errors
        assert errors == []

    def test_per_task_run_command_list_rejected(self, tmp_path: Path):
        body = _SCHEMA_BODY.replace(
            "## Acceptance\n- the check is added\n",
            "## Acceptance\naccepts: [unit]\nrun: [pytest]\n",
        )

        errors = _repair_errors(tmp_path, body, declared_profile="unit")

        # Per-task command lists are forbidden even when the referenced profile
        # is declared: verification commands are global at the project level.
        assert "task.acceptance_run_forbidden" in errors
