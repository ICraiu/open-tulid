from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_tulid.cli.main import app
from open_tulid.config import load_config
from open_tulid.models import Config
from open_tulid.vault.validator import validate_vault

runner = CliRunner()


def _make_config(vault_root: Path, projects: list[str]) -> Path:
    cfg = vault_root / ".open-tulid.toml"
    cfg.write_text(
        f'[vault]\nroot = "{vault_root}"\nprojects = {projects!r}\n',
        encoding="utf-8",
    )
    return cfg


def _make_project(
    vault_root: Path,
    name: str,
    kanban_files: dict[str, str] | None = None,
) -> Path:
    project_dir = vault_root / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "kanban").mkdir(parents=True, exist_ok=True)
    (project_dir / "docs").mkdir(parents=True, exist_ok=True)
    (project_dir / "tasks").mkdir(parents=True, exist_ok=True)

    if kanban_files:
        for fname, content in kanban_files.items():
            (project_dir / "kanban" / fname).write_text(content, encoding="utf-8")

    return project_dir


def _make_task(vault_root: Path, project_name: str, task_name: str) -> Path:
    task_dir = vault_root / project_name / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_file = task_dir / f"{task_name}.md"
    task_file.write_text(
        f"## Idea\n\nIdea: {task_name}\n",
        encoding="utf-8",
    )
    return task_file


class TestVaultValidateHappyPath:
    def test_single_valid_project(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": (
                "## Todo\n"
                "- [ ] [[Task 1]]\n"
            ),
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
            assert "Checked 1 projects." in result.output
        finally:
            os.chdir(original)

    def test_multiple_valid_projects(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent", "Game"])
        for proj in ["Agent", "Game"]:
            _make_project(tmp_path, proj, kanban_files={
                "Work.md": (
                    "## Todo\n"
                    "- [ ] [[Task 1]]\n"
                ),
            })
            _make_task(tmp_path, proj, "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
            assert "Checked 2 projects." in result.output
        finally:
            os.chdir(original)

    def test_kanban_with_frontmatter(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": (
                "---\n"
                "kanban-plugin: board\n"
                "---\n"
                "## Todo\n"
                "- [ ] [[Task 1]]\n"
            ),
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
        finally:
            os.chdir(original)

    def test_kanban_with_settings_block(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": (
                "## Todo\n"
                "- [ ] [[Task 1]]\n"
                "\n"
                "%% kanban:settings\n"
                '{"kanban-plugin":"board"}\n'
                "%%\n"
            ),
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
        finally:
            os.chdir(original)

    def test_task_row_dash_checkbox(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
        finally:
            os.chdir(original)

    def test_task_row_dash_link(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
        finally:
            os.chdir(original)

    def test_task_row_link_only(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n[[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
        finally:
            os.chdir(original)

    def test_task_links_scoped_to_project(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent", "Game"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_project(tmp_path, "Game", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")
        _make_task(tmp_path, "Game", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
        finally:
            os.chdir(original)

    def test_ignored_directories_not_checked(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")
        # Create an unmanaged directory
        (tmp_path / "Unmanaged").mkdir()

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
        finally:
            os.chdir(original)


class TestVaultValidateFailures:
    def test_config_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 2
        finally:
            os.chdir(original)

    def test_project_missing_from_vault(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "does not exist" in result.output
        finally:
            os.chdir(original)

    def test_project_missing_kanban(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        (tmp_path / "Agent").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Agent" / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Agent" / "tasks").mkdir(parents=True, exist_ok=True)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "missing required directory: kanban/" in result.output
        finally:
            os.chdir(original)

    def test_project_missing_docs(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        (tmp_path / "Agent").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Agent" / "kanban").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Agent" / "tasks").mkdir(parents=True, exist_ok=True)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "missing required directory: docs/" in result.output
        finally:
            os.chdir(original)

    def test_project_missing_tasks(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        (tmp_path / "Agent").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Agent" / "kanban").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Agent" / "docs").mkdir(parents=True, exist_ok=True)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "missing required directory: tasks/" in result.output
        finally:
            os.chdir(original)

    def test_kanban_subdirectory_fails(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent")
        (tmp_path / "Agent" / "kanban" / "archive").mkdir()

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Subdirectory found in kanban/" in result.output
        finally:
            os.chdir(original)

    def test_kanban_non_md_file_fails(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent")
        (tmp_path / "Agent" / "kanban" / "notes.txt").write_text("hello", encoding="utf-8")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Non-Markdown file in kanban/" in result.output
        finally:
            os.chdir(original)

    def test_unclosed_frontmatter(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "---\nfoo: bar\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Unclosed frontmatter" in result.output
        finally:
            os.chdir(original)

    def test_unclosed_kanban_settings(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": (
                "## Todo\n"
                "- [ ] [[Task 1]]\n"
                "%% kanban:settings\n"
                '{"kanban-plugin":"board"}\n'
            ),
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Unclosed kanban settings" in result.output
        finally:
            os.chdir(original)

    def test_content_before_first_section(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "random text\n## Todo\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_task_row_before_section(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "- [ ] [[Task 1]]\n## Todo\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "appears before any section" in result.output
        finally:
            os.chdir(original)

    def test_task_row_no_link(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] Task 1\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_task_row_extra_text(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]] extra\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_task_row_multiple_links(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]] [[Task 2]]\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_task_row_checked_checkbox(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [x] [[Task 1]]\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_task_link_with_alias(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1|alias]]\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_task_link_with_section(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1#Section]]\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_task_link_with_path(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Agent/tasks/Task 1]]\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)

    def test_missing_task_file(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Missing Task]]\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Linked task file does not exist: [[Missing Task]]" in result.output
        finally:
            os.chdir(original)

    def test_task_in_different_project_not_found(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent", "Game"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_project(tmp_path, "Game", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        # Only create task in Game, not Agent
        _make_task(tmp_path, "Game", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Linked task file does not exist: [[Task 1]]" in result.output
        finally:
            os.chdir(original)

    def test_validation_report_counts(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent", "Game"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": (
                "## Todo\n"
                "- [ ] [[Task 1]]\n"
                "- [ ] [[Task 2]]\n"
            ),
        })
        _make_project(tmp_path, "Game", kanban_files={
            "Board.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")
        _make_task(tmp_path, "Agent", "Task 2")
        _make_task(tmp_path, "Game", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Checked 2 projects." in result.output
            assert "Checked 2 kanban files." in result.output
            assert "Checked 3 task links." in result.output
        finally:
            os.chdir(original)

    def test_non_task_content_in_section(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\nsome random text\n",
        })

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Task row must contain" in result.output
        finally:
            os.chdir(original)


def _make_idea_task_file(vault_root: Path, project_name: str, name: str, content: str = "") -> Path:
    task_dir = vault_root / project_name / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_file = task_dir / f"{name}.md"
    if not content:
        content = f"## Idea\n\nIdea: {name}\n"
    task_file.write_text(content, encoding="utf-8")
    return task_file


def _make_technical_direction_file(vault_root: Path, project_name: str, name: str, content: str | None = None) -> Path:
    docs_dir = vault_root / project_name / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_file = docs_dir / f"{name}.md"
    if content is None:
        content = f"## Direction\n\nDirection: {name}\n"
    doc_file.write_text(content, encoding="utf-8")
    return doc_file


class TestVaultDomainValidate:
    def test_valid_idea_task_linked_from_kanban(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
        finally:
            os.chdir(original)

    def test_empty_task_file_fails(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        (tmp_path / "Agent" / "tasks" / "Task 1.md").write_text("", encoding="utf-8")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "supported task artifact" in result.output
        finally:
            os.chdir(original)

    def test_defined_task_links_to_valid_technical_direction(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_technical_direction_file(tmp_path, "Agent", "Technical direction")
        task_content = (
            "## Idea\n\nIdea: Define task\n"
            "## Technical direction\n\nDirection: docs/Technical direction.md\n"
        )
        (tmp_path / "Agent" / "tasks" / "Task 1.md").write_text(
            task_content, encoding="utf-8"
        )

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
        finally:
            os.chdir(original)

    def test_defined_task_missing_linked_doc_fails(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        task_content = (
            "## Idea\n\nIdea: Define task\n"
            "## Technical direction\n\nDirection: docs/Missing direction.md\n"
        )
        _make_idea_task_file(tmp_path, "Agent", "Task 1", task_content)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "not found in registry" in result.output
        finally:
            os.chdir(original)

    def test_invalid_linked_doc_fails(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_technical_direction_file(tmp_path, "Agent", "Technical direction", "")
        task_content = (
            "## Idea\n\nIdea: Define task\n"
            "## Technical direction\n\nDirection: docs/Technical direction.md\n"
        )
        (tmp_path / "Agent" / "tasks" / "Task 1.md").write_text(
            task_content, encoding="utf-8"
        )

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "not found in registry" not in result.output
            assert "Domain artifact validation failed" in result.output
        finally:
            os.chdir(original)

    def test_completed_task_done_with_proof_passes(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        task_content = (
            "## Idea\n\nIdea: Complete task\n"
            "## Status\n\nStatus: done\n"
            "## Proof\n\nEvidence: Tests pass\n"
            "Changed files: tasks/Task 1.md\n"
            "Validation result: pytest\n"
        )
        _make_idea_task_file(tmp_path, "Agent", "Task 1", task_content)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
        finally:
            os.chdir(original)

    def test_arbitrary_unreferenced_docs_ignored(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_task(tmp_path, "Agent", "Task 1")
        (tmp_path / "Agent" / "docs" / "random.md").write_text(
            "## Random\n\nRandom content\n", encoding="utf-8"
        )

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
        finally:
            os.chdir(original)

    def test_referenced_doc_validated(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_technical_direction_file(tmp_path, "Agent", "Technical direction")
        task_content = (
            "## Idea\n\nIdea: Define task\n"
            "## Technical direction\n\nDirection: docs/Technical direction.md\n"
        )
        _make_idea_task_file(tmp_path, "Agent", "Task 1", task_content)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
        finally:
            os.chdir(original)

    def test_file_link_aliases_work(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        _make_technical_direction_file(tmp_path, "Agent", "Technical direction")
        task_content = (
            "## Idea\n\nIdea: Define task\n"
            "## Technical direction\n\nDirection: Agent/docs/Technical direction.md\n"
        )
        _make_idea_task_file(tmp_path, "Agent", "Task 1", task_content)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 0
            assert "Vault validation passed." in result.output
        finally:
            os.chdir(original)

    def test_domain_errors_cause_exit_code_1(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        (tmp_path / "Agent" / "tasks" / "Task 1.md").write_text("", encoding="utf-8")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "Domain artifact validation failed" in result.output
        finally:
            os.chdir(original)

    def test_non_utf8_task_file_fails(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        (tmp_path / "Agent" / "tasks" / "Task 1.md").write_bytes(b"\xff\xfe")

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "not valid UTF-8" in result.output
        finally:
            os.chdir(original)

    def test_nested_doc_reference_not_read(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        nested_dir = tmp_path / "Agent" / "docs" / "nested"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / "Direction.md").write_text(
            "## Direction\n\nDirection: nested direction\n", encoding="utf-8"
        )
        task_content = (
            "## Idea\n\nIdea: Define task\n"
            "## Technical direction\n\nDirection: docs/nested/Direction.md\n"
        )
        _make_idea_task_file(tmp_path, "Agent", "Task 1", task_content)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "not found in registry" in result.output
        finally:
            os.chdir(original)

    def test_invalid_vault_relative_doc_fails_with_doc_error(self, tmp_path: Path):
        _make_config(tmp_path, ["Agent"])
        _make_project(tmp_path, "Agent", kanban_files={
            "Work.md": "## Todo\n- [ ] [[Task 1]]\n",
        })
        (tmp_path / "Agent" / "docs" / "Technical direction.md").write_text(
            "", encoding="utf-8"
        )
        task_content = (
            "## Idea\n\nIdea: Define task\n"
            "## Technical direction\n\nDirection: Agent/docs/Technical direction.md\n"
        )
        _make_idea_task_file(tmp_path, "Agent", "Task 1", task_content)

        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(app, ["vault", "validate"])
            assert result.exit_code == 1
            assert "not found in registry" not in result.output
            assert "Domain artifact validation failed" in result.output
        finally:
            os.chdir(original)
