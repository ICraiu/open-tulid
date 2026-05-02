from __future__ import annotations

import os
import tempfile

import pytest

from open_tulid.domain.schema import (
    Artifact,
    ArtifactRegistry,
    ArtifactState,
    Field,
    FieldType,
    Section,
    Template,
)
from open_tulid.domain.templates import (
    build_completed_task_template,
    build_defined_task_template,
    build_idea_task_template,
    build_technical_direction_template,
)
from open_tulid.domain.writers import (
    serialize_artifact_content,
    write_artifact_file,
)


def _make_idea_artifact(path: str = "/idea.md") -> Artifact:
    tpl = build_idea_task_template()
    return Artifact(
        path=path,
        state=ArtifactState.IdeaTask,
        template=tpl,
        sections=[Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Add vault validation")])],
    )


def _make_defined_artifact(path: str = "/task.md") -> Artifact:
    tpl = build_defined_task_template()
    return Artifact(
        path=path,
        state=ArtifactState.DefinedTask,
        template=tpl,
        sections=[
            Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Add vault validation")]),
            Section(name="Technical direction", fields=[Field(name="Direction", type=FieldType.FILE, value="Agent/docs/Technical direction.md")]),
        ],
    )


def _make_link_registry() -> ArtifactRegistry:
    reg = ArtifactRegistry()
    direction_tpl = build_technical_direction_template()
    reg.register(Artifact(
        path="Agent/docs/Technical direction.md",
        state=ArtifactState.TechnicalDirection,
        template=direction_tpl,
        sections=[Section(name="Direction", fields=[Field(name="Direction", type=FieldType.STRING, value="Use agent")])],
    ))
    reg.register(Artifact(
        path="/a.md",
        state=ArtifactState.IdeaTask,
        template=build_idea_task_template(),
        sections=[Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="a")])],
    ))
    reg.register(Artifact(
        path="/b.md",
        state=ArtifactState.IdeaTask,
        template=build_idea_task_template(),
        sections=[Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="b")])],
    ))
    return reg


class TestValidWriters:
    def test_write_valid_idea_task(self):
        art = _make_idea_artifact()
        result = serialize_artifact_content(art)
        assert result.report.is_valid is True
        assert result.content is not None
        assert "## Idea" in result.content
        assert "Idea: Add vault validation" in result.content

    def test_write_valid_defined_task(self):
        art = _make_defined_artifact()
        result = serialize_artifact_content(art, _make_link_registry())
        assert result.report.is_valid is True
        assert result.content is not None
        assert "## Idea" in result.content
        assert "## Technical direction" in result.content
        assert "Idea: Add vault validation" in result.content
        assert "Direction: Agent/docs/Technical direction.md" in result.content

    def test_write_to_file_path(self):
        art = _make_idea_artifact()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "idea.md")
            result = write_artifact_file(art, path=out_path)
            assert result.report.is_valid is True
            assert result.path == out_path
            assert result.content is not None
            with open(out_path, "r", encoding="utf-8") as f:
                written = f.read()
            assert "## Idea" in written
            assert "Idea: Add vault validation" in written

    def test_write_file_list_field(self):
        tpl = build_completed_task_template()
        art = Artifact(
            path="/task.md",
            state=ArtifactState.CompletedTask,
            template=tpl,
            sections=[
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[
                    Field(name="Evidence", type=FieldType.STRING, value="done"),
                    Field(name="Changed files", type=FieldType.FILE_LIST, value=["/a.md", "/b.md"]),
                    Field(name="Validation result", type=FieldType.STRING, value="ok"),
                ]),
            ],
        )
        result = serialize_artifact_content(art, _make_link_registry())
        assert result.report.is_valid is True
        assert result.content is not None
        assert "Changed files: /a.md, /b.md" in result.content

    def test_roundtrip(self):
        from open_tulid.domain.readers import parse_artifact_content

        art = _make_idea_artifact()
        write_result = serialize_artifact_content(art)
        assert write_result.report.is_valid is True
        tpl = build_idea_task_template()
        read_result = parse_artifact_content(art.path, write_result.content, ArtifactState.IdeaTask, tpl)
        assert read_result.report.is_valid is True
        assert read_result.artifact is not None
        assert read_result.artifact.sections[0].fields[0].value == "Add vault validation"

    def test_write_optional_section_absent(self):
        tpl = build_completed_task_template()
        art = Artifact(
            path="/task.md",
            state=ArtifactState.CompletedTask,
            template=tpl,
            sections=[
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="in progress")]),
            ],
        )
        result = serialize_artifact_content(art)
        assert result.report.is_valid is True
        assert result.content is not None
        assert "## Idea" in result.content
        assert "## Status" in result.content


class TestInvalidWriters:
    def test_empty_path(self):
        art = _make_idea_artifact(path="")
        result = serialize_artifact_content(art)
        assert result.report.is_valid is False
        assert any("path" in e.location for e in result.report.errors)

    def test_state_mismatch(self):
        tpl = build_idea_task_template()
        art = Artifact(
            path="/foo.md",
            state=ArtifactState.DefinedTask,
            template=tpl,
            sections=[Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")])],
        )
        result = serialize_artifact_content(art)
        assert result.report.is_valid is False
        assert any("state" in e.message for e in result.report.errors)

    def test_missing_required_section(self):
        tpl = build_defined_task_template()
        art = Artifact(
            path="/foo.md",
            state=ArtifactState.DefinedTask,
            template=tpl,
            sections=[Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")])],
        )
        result = serialize_artifact_content(art)
        assert result.report.is_valid is False

    def test_missing_required_field(self):
        tpl = build_idea_task_template()
        art = Artifact(
            path="/foo.md",
            state=ArtifactState.IdeaTask,
            template=tpl,
            sections=[Section(name="Idea", fields=[])],
        )
        result = serialize_artifact_content(art)
        assert result.report.is_valid is False

    def test_invalid_linked_file(self):
        tpl = build_defined_task_template()
        reg = ArtifactRegistry()
        art = Artifact(
            path="/foo.md",
            state=ArtifactState.DefinedTask,
            template=tpl,
            sections=[
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Technical direction", fields=[Field(name="Direction", type=FieldType.FILE, value="/missing.md")]),
            ],
        )
        result = serialize_artifact_content(art, reg)
        assert result.report.is_valid is False
        assert any("not found in registry" in e.message for e in result.report.errors)

    def test_invalid_file_list(self):
        tpl = build_completed_task_template()
        reg = ArtifactRegistry()
        art = Artifact(
            path="/foo.md",
            state=ArtifactState.CompletedTask,
            template=tpl,
            sections=[
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[
                    Field(name="Evidence", type=FieldType.STRING, value="done"),
                    Field(name="Changed files", type=FieldType.FILE_LIST, value=["/missing.md"]),
                    Field(name="Validation result", type=FieldType.STRING, value="ok"),
                ]),
            ],
        )
        result = serialize_artifact_content(art, reg)
        assert result.report.is_valid is False
        assert any("not found in registry" in e.message for e in result.report.errors)

    def test_completed_task_done_no_proof(self):
        tpl = build_completed_task_template()
        art = Artifact(
            path="/foo.md",
            state=ArtifactState.CompletedTask,
            template=tpl,
            sections=[
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
            ],
        )
        result = serialize_artifact_content(art)
        assert result.report.is_valid is False
        assert any("Proof" in e.message for e in result.report.errors)

    def test_write_to_missing_parent_directory(self):
        art = _make_idea_artifact()
        with tempfile.TemporaryDirectory() as tmpdir:
            deep_path = os.path.join(tmpdir, "nonexistent", "deep", "idea.md")
            result = write_artifact_file(art, path=deep_path)
            assert result.report.is_valid is False
            assert any("Parent directory" in e.message for e in result.report.errors)

    def test_write_io_failure_clears_path_and_content(self):
        art = _make_idea_artifact()
        with tempfile.TemporaryDirectory() as tmpdir:
            deep_path = os.path.join(tmpdir, "nonexistent", "deep", "file.md")
            result = write_artifact_file(art, path=deep_path)
            assert result.report.is_valid is False
            assert result.path is None
            assert result.content is None

    def test_write_without_explicit_path(self):
        art = _make_idea_artifact()
        with tempfile.TemporaryDirectory() as tmpdir:
            art.path = os.path.join(tmpdir, "idea.md")
            result = write_artifact_file(art)
            assert result.report.is_valid is True
            assert result.path == art.path
            assert result.content is not None
            with open(art.path, "r", encoding="utf-8") as f:
                written = f.read()
            assert "## Idea" in written

    def test_write_uses_artifact_path_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            art = _make_idea_artifact()
            art.path = os.path.join(tmpdir, "default.md")
            result = write_artifact_file(art)
            assert result.report.is_valid is True
            assert result.path == art.path
            assert os.path.exists(art.path)

    def test_idea_task_roundtrip(self):
        from open_tulid.domain.readers import parse_artifact_content

        art = _make_idea_artifact()
        write_result = serialize_artifact_content(art)
        assert write_result.report.is_valid is True
        tpl = build_idea_task_template()
        read_result = parse_artifact_content(art.path, write_result.content, ArtifactState.IdeaTask, tpl)
        assert read_result.report.is_valid is True
        assert read_result.artifact is not None
        assert read_result.artifact.sections[0].fields[0].value == "Add vault validation"

    def test_defined_task_roundtrip(self):
        from open_tulid.domain.readers import parse_artifact_content

        art = _make_defined_artifact()
        reg = _make_link_registry()
        write_result = serialize_artifact_content(art, reg)
        assert write_result.report.is_valid is True
        tpl = build_defined_task_template()
        read_result = parse_artifact_content(art.path, write_result.content, ArtifactState.DefinedTask, tpl, reg)
        assert read_result.report.is_valid is True
        assert read_result.artifact is not None
        idea_field = read_result.artifact.sections[0].fields[0]
        assert idea_field.value == "Add vault validation"

    def test_completed_task_roundtrip(self):
        from open_tulid.domain.readers import parse_artifact_content

        tpl = build_completed_task_template()
        reg = _make_link_registry()
        art = Artifact(
            path="/task.md",
            state=ArtifactState.CompletedTask,
            template=tpl,
            sections=[
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test task")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[
                    Field(name="Evidence", type=FieldType.STRING, value="tests pass"),
                    Field(name="Changed files", type=FieldType.FILE_LIST, value=["/a.md", "/b.md"]),
                    Field(name="Validation result", type=FieldType.STRING, value="all good"),
                ]),
            ],
        )
        write_result = serialize_artifact_content(art, reg)
        assert write_result.report.is_valid is True
        read_result = parse_artifact_content(art.path, write_result.content, ArtifactState.CompletedTask, tpl, reg)
        assert read_result.report.is_valid is True
        assert read_result.artifact is not None
        status_field = read_result.artifact.sections[1].fields[0]
        assert status_field.value == "done"

    def test_unknown_section_preserved_in_read(self):
        from open_tulid.domain.readers import parse_artifact_content

        content = """## Idea

Idea: Add vault validation

## Notes

Notes: some extra info
"""
        tpl = build_idea_task_template()
        result = parse_artifact_content("/foo.md", content, ArtifactState.IdeaTask, tpl)
        assert result.report.is_valid is True
        assert result.artifact is not None
        assert len(result.artifact.sections) == 2
        assert result.artifact.sections[1].name == "Notes"
        assert len(result.artifact.sections[1].fields) == 1
        assert result.artifact.sections[1].fields[0].name == "Notes"

    def test_unknown_section_emitted_by_writer(self):
        tpl = build_idea_task_template()
        art = Artifact(
            path="/foo.md",
            state=ArtifactState.IdeaTask,
            template=tpl,
            sections=[
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Notes", fields=[Field(name="Notes", type=FieldType.STRING, value="extra")]),
            ],
        )
        result = serialize_artifact_content(art)
        assert result.report.is_valid is True
        assert result.content is not None
        assert "## Idea" in result.content
        assert "## Notes" in result.content
        assert "Notes: extra" in result.content
