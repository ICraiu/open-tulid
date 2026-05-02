from __future__ import annotations

import os
import tempfile

import pytest

from open_tulid.domain.schema import (
    ArtifactRegistry,
    ArtifactState,
    FieldType,
)
from open_tulid.domain.templates import (
    build_completed_task_template,
    build_defined_task_template,
    build_idea_task_template,
    build_technical_direction_template,
)
from open_tulid.domain.readers import (
    parse_artifact_content,
    read_artifact_file,
)


IDEA_CONTENT = """## Idea

Idea: Add vault validation
"""

DEFINED_CONTENT = """## Idea

Idea: Add vault validation

## Technical direction

Direction: Agent/docs/Technical direction.md
"""


def _make_registry() -> ArtifactRegistry:
    reg = ArtifactRegistry()
    direction_tpl = build_technical_direction_template()
    direction_art = reg.artifacts_by_path
    from open_tulid.domain.schema import Artifact, Section, Field
    direction_art["Agent/docs/Technical direction.md"] = Artifact(
        path="Agent/docs/Technical direction.md",
        state=ArtifactState.TechnicalDirection,
        template=direction_tpl,
        sections=[Section(name="Direction", fields=[Field(name="Direction", type=FieldType.STRING, value="Use agent")])],
    )
    return reg


class TestValidReaders:
    def test_read_valid_idea_task(self):
        tpl = build_idea_task_template()
        result = parse_artifact_content("/idea.md", IDEA_CONTENT, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is True
        assert result.artifact is not None
        assert result.artifact.path == "/idea.md"
        assert result.artifact.state == ArtifactState.IdeaTask
        assert len(result.artifact.sections) == 1
        assert result.artifact.sections[0].name == "Idea"
        assert len(result.artifact.sections[0].fields) == 1
        assert result.artifact.sections[0].fields[0].name == "Idea"
        assert result.artifact.sections[0].fields[0].value == "Add vault validation"

    def test_read_valid_defined_task(self):
        tpl = build_defined_task_template()
        reg = _make_registry()
        result = parse_artifact_content("/task.md", DEFINED_CONTENT, ArtifactState.DefinedTask, tpl, reg)
        assert result.report.is_valid is True
        assert result.artifact is not None
        assert len(result.artifact.sections) == 2

    def test_read_same_path_different_state(self):
        tpl1 = build_idea_task_template()
        tpl2 = build_defined_task_template()
        result1 = parse_artifact_content("/same.md", IDEA_CONTENT, ArtifactState.IdeaTask, tpl1)
        assert result1.report.is_valid is True
        assert result1.artifact.state == ArtifactState.IdeaTask

    def test_read_existing_utf8_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(IDEA_CONTENT)
            tmp_path = f.name
        try:
            tpl = build_idea_task_template()
            result = read_artifact_file(tmp_path, ArtifactState.IdeaTask, tpl)
            assert result.report.is_valid is True
            assert result.artifact is not None
        finally:
            os.unlink(tmp_path)

    def test_read_file_list_content(self):
        content = """## Idea

Idea: test task

## Status

Status: done

## Proof

Changed files: /a.md, /b.md
Evidence: tests pass
Validation result: all good
"""
        tpl = build_completed_task_template()
        reg = _make_registry()
        reg.artifacts_by_path["/a.md"] = reg.artifacts_by_path["Agent/docs/Technical direction.md"]
        reg.artifacts_by_path["/b.md"] = reg.artifacts_by_path["Agent/docs/Technical direction.md"]
        result = parse_artifact_content("/proof.md", content, ArtifactState.CompletedTask, tpl, reg)
        assert result.report.is_valid is True

    def test_read_single_item_file_list_content(self):
        content = """## Idea

Idea: test task

## Status

Status: done

## Proof

Changed files: /a.md
Evidence: tests pass
Validation result: all good
"""
        tpl = build_completed_task_template()
        reg = _make_registry()
        reg.artifacts_by_path["/a.md"] = reg.artifacts_by_path["Agent/docs/Technical direction.md"]
        result = parse_artifact_content("/proof.md", content, ArtifactState.CompletedTask, tpl, reg)
        assert result.report.is_valid is True
        assert result.artifact is not None
        changed_files = result.artifact.sections[2].fields[0]
        assert changed_files.type == FieldType.FILE_LIST
        assert changed_files.value == ["/a.md"]


class TestInvalidReaders:
    def test_empty_path(self):
        tpl = build_idea_task_template()
        result = parse_artifact_content("", IDEA_CONTENT, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is False
        assert any("Path must be non-empty" in e.message for e in result.report.errors)

    def test_missing_file(self):
        tpl = build_idea_task_template()
        result = read_artifact_file("/nonexistent/file.md", ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is False
        assert any("not found" in e.message for e in result.report.errors)

    def test_non_utf8_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\xff\xfe")
            tmp_path = f.name
        try:
            tpl = build_idea_task_template()
            result = read_artifact_file(tmp_path, ArtifactState.IdeaTask, tpl)
            assert result.report.is_valid is False
            assert any("not valid UTF-8" in e.message for e in result.report.errors)
        finally:
            os.unlink(tmp_path)

    def test_state_mismatch(self):
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", IDEA_CONTENT, ArtifactState.DefinedTask, tpl)
        assert result.report.is_valid is False
        assert any("does not match template state" in e.message for e in result.report.errors)

    def test_preamble_text(self):
        content = "Some preamble text\n\n## Idea\n\nIdea: test\n"
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", content, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is False
        assert any("before the first section" in e.message for e in result.report.errors)

    def test_duplicate_sections(self):
        content = """## Idea

Idea: first

## Idea

Idea: second
"""
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", content, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is False
        assert any("Duplicate section" in e.message for e in result.report.errors)

    def test_duplicate_fields(self):
        content = """## Idea

Idea: first
Idea: second
"""
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", content, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is False
        assert any("Duplicate field" in e.message for e in result.report.errors)

    def test_missing_required_section(self):
        content = ""
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", content, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is False
        assert any("Required section" in e.message for e in result.report.errors)

    def test_empty_idea_field(self):
        content = """## Idea

Idea:
"""
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", content, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is False
        assert any("non-empty text" in e.message for e in result.report.errors)

    def test_missing_direction_link(self):
        tpl = build_defined_task_template()
        reg = ArtifactRegistry()
        result = parse_artifact_content("/foo.md", DEFINED_CONTENT, ArtifactState.DefinedTask, tpl, reg)
        assert result.report.is_valid is False
        assert any("not found in registry" in e.message for e in result.report.errors)

    def test_file_list_missing_registry(self):
        content = """## Proof

Changed files: /missing.md
"""
        tpl = build_completed_task_template()
        reg = ArtifactRegistry()
        result = parse_artifact_content("/foo.md", content, ArtifactState.CompletedTask, tpl, reg)
        assert result.report.is_valid is False
        assert any("not found in registry" in e.message for e in result.report.errors)

    def test_read_idea_content_as_defined_task(self):
        tpl = build_defined_task_template()
        reg = _make_registry()
        result = parse_artifact_content("/foo.md", IDEA_CONTENT, ArtifactState.DefinedTask, tpl, reg)
        assert result.report.is_valid is False

    def test_unknown_field_preserved_in_read(self):
        content = """## Idea

Idea: Add vault validation
ExtraField: some value, with comma
"""
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", content, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is True
        assert result.artifact is not None
        assert len(result.artifact.sections[0].fields) == 2
        assert result.artifact.sections[0].fields[1].type == FieldType.STRING
        assert result.artifact.sections[0].fields[1].value == "some value, with comma"
