from __future__ import annotations

from pathlib import Path
import os

from open_tulid.workflow.runtime import clear_workflow_cache, load_workflow_definition


def test_load_workflow_definition_caches_by_path_and_mtime(tmp_path: Path):
    clear_workflow_cache()
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "statements:\n"
        "  - kind: state\n"
        "    id: Todo\n",
        encoding="utf-8",
    )

    first = load_workflow_definition(workflow)
    second = load_workflow_definition(workflow)

    assert first.valid is True
    assert first is second
    assert first.definition is not None
    assert "Todo" in first.definition.states


def test_load_workflow_definition_reports_missing_file(tmp_path: Path):
    clear_workflow_cache()

    result = load_workflow_definition(tmp_path / "missing.yaml")

    assert result.valid is False
    assert result.definition is None
    assert result.diagnostics[0].code == "workflow.load.failed"


def test_load_workflow_definition_uses_content_fingerprint_not_mtime(tmp_path: Path):
    clear_workflow_cache()
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "statements:\n"
        "  - kind: state\n"
        "    id: Todo\n",
        encoding="utf-8",
    )
    first = load_workflow_definition(workflow)
    original_stat = workflow.stat()

    workflow.write_text(
        "schema_version: 1\n"
        "statements:\n"
        "  - kind: state\n"
        "    id: Done\n",
        encoding="utf-8",
    )
    os.utime(workflow, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = load_workflow_definition(workflow)

    assert first is not second
    assert second.definition is not None
    assert "Done" in second.definition.states
    assert "Todo" not in second.definition.states


def test_load_workflow_definition_validates_instruction_refs_inside_project_agents(tmp_path: Path):
    clear_workflow_cache()
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "statements:\n"
        "  - kind: worker\n"
        "    id: codex\n"
        "    instructions: [missing]\n",
        encoding="utf-8",
    )

    result = load_workflow_definition(workflow)

    assert result.valid is False
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["instructions.not_found"]


def test_load_workflow_definition_accepts_existing_agent_instruction(tmp_path: Path):
    clear_workflow_cache()
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "default.agent.md").write_text("Do good work.\n", encoding="utf-8")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "statements:\n"
        "  - kind: worker\n"
        "    id: codex\n"
        "    instructions: [default]\n",
        encoding="utf-8",
    )

    result = load_workflow_definition(workflow)

    assert result.valid is True
