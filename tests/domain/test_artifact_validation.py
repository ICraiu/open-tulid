from __future__ import annotations

import pytest

from open_tulid.domain.schema import (
    Artifact,
    ArtifactRegistry,
    ArtifactState,
    Field,
    FieldType,
    Section,
    Template,
    ValidatorType,
)
from open_tulid.domain.templates import (
    build_completed_task_template,
    build_defined_task_template,
    build_idea_task_template,
    build_technical_direction_template,
)
from open_tulid.domain.validation import validate_artifact


def _make_artifact(path: str, state: ArtifactState, template: Template, sections: list[Section]) -> Artifact:
    return Artifact(path=path, state=state, template=template, sections=sections)


def _make_registry(
    idea_path: str = "/idea.md",
    direction_path: str = "/direction.md",
) -> ArtifactRegistry:
    reg = ArtifactRegistry()
    idea_tpl = build_idea_task_template()
    direction_tpl = build_technical_direction_template()

    idea_art = Artifact(path=idea_path, state=ArtifactState.IdeaTask, template=idea_tpl, sections=[
        Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Do something")]),
    ])
    reg.register(idea_art)

    direction_art = Artifact(path=direction_path, state=ArtifactState.TechnicalDirection, template=direction_tpl, sections=[
        Section(name="Direction", fields=[Field(name="Direction", type=FieldType.STRING, value="Use agent")]),
    ])
    reg.register(direction_art)

    return reg


class TestValidArtifacts:
    def test_valid_idea_task(self):
        tpl = build_idea_task_template()
        art = _make_artifact(
            "/idea.md",
            ArtifactState.IdeaTask,
            tpl,
            [Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Add vault validation")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is True

    def test_valid_technical_direction(self):
        tpl = build_technical_direction_template()
        art = _make_artifact(
            "/direction.md",
            ArtifactState.TechnicalDirection,
            tpl,
            [Section(name="Direction", fields=[Field(name="Direction", type=FieldType.STRING, value="Use agent")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is True

    def test_valid_defined_task(self):
        tpl = build_defined_task_template()
        reg = _make_registry()
        art = _make_artifact(
            "/task.md",
            ArtifactState.DefinedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Add vault validation")]),
                Section(name="Technical direction", fields=[Field(name="Direction", type=FieldType.FILE, value="/direction.md")]),
            ],
        )
        report = validate_artifact(art, reg)
        assert report.is_valid is True

    def test_completed_task_done_with_proof(self):
        tpl = build_completed_task_template()
        reg = _make_registry()
        art = _make_artifact(
            "/completed.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Add vault validation")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(
                    name="Proof",
                    fields=[
                        Field(name="Evidence", type=FieldType.STRING, value="Tests pass"),
                        Field(name="Changed files", type=FieldType.FILE_LIST, value=["/direction.md"]),
                        Field(name="Validation result", type=FieldType.STRING, value="All tests pass"),
                    ],
                ),
            ],
        )
        report = validate_artifact(art, reg)
        assert report.is_valid is True

    def test_completed_task_not_done_without_proof(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/in_progress.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Add vault validation")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="in progress")]),
            ],
        )
        report = validate_artifact(art)
        assert report.is_valid is True

    def test_file_list_valid(self):
        from open_tulid.domain.schema import FieldTemplate, SectionTemplate

        tpl = Template(
            name="DefinedTask",
            state=ArtifactState.DefinedTask,
            sections=[
                SectionTemplate(
                    name="Idea",
                    required=True,
                    fields=[
                        FieldTemplate(name="Idea", type=FieldType.STRING, required=True),
                    ],
                ),
                SectionTemplate(
                    name="Technical direction",
                    required=True,
                    fields=[
                        FieldTemplate(
                            name="Direction",
                            type=FieldType.FILE_LIST,
                            required=True,
                            validators=[ValidatorType.FILE_LINK_EXISTS],
                        ),
                    ],
                ),
            ],
        )
        reg = _make_registry()
        art = _make_artifact(
            "/task.md",
            ArtifactState.DefinedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="Test")]),
                Section(name="Technical direction", fields=[Field(name="Direction", type=FieldType.FILE_LIST, value=["/direction.md", "/idea.md"])]),
            ],
        )
        report = validate_artifact(art, reg)
        assert report.is_valid is True


class TestInvalidArtifactSchema:
    def test_empty_path(self):
        tpl = build_idea_task_template()
        art = Artifact(path="", state=ArtifactState.IdeaTask, template=tpl)
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("path" in e.location for e in report.errors)

    def test_state_mismatch(self):
        tpl = build_idea_task_template()
        art = Artifact(path="/foo.md", state=ArtifactState.DefinedTask, template=tpl)
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("state" in e.location for e in report.errors)

    def test_empty_section_name(self):
        tpl = build_idea_task_template()
        art = _make_artifact("/foo.md", ArtifactState.IdeaTask, tpl, [Section(name="", fields=[])])
        report = validate_artifact(art)
        assert report.is_valid is False

    def test_duplicate_field_names_in_section(self):
        tpl = build_idea_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.IdeaTask,
            tpl,
            [Section(name="Idea", fields=[
                Field(name="Idea", type=FieldType.STRING, value="a"),
                Field(name="Idea", type=FieldType.STRING, value="b"),
            ])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("Duplicate field" in e.message for e in report.errors)

    def test_empty_field_name(self):
        tpl = build_idea_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.IdeaTask,
            tpl,
            [Section(name="Idea", fields=[Field(name="", type=FieldType.STRING, value="a")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False

    def test_string_field_with_list_value(self):
        tpl = build_idea_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.IdeaTask,
            tpl,
            [Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value=["a"])])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("must have a string value" in e.message for e in report.errors)

    def test_status_field_with_list_value(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value=["a"])])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False

    def test_file_field_with_list_value(self):
        tpl = build_defined_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.DefinedTask,
            tpl,
            [Section(name="Technical direction", fields=[Field(name="Direction", type=FieldType.FILE, value=["a"])])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False

    def test_file_list_field_with_string_value(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [Section(name="Proof", fields=[Field(name="Changed files", type=FieldType.FILE_LIST, value="a.md")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("must have a list value" in e.message for e in report.errors)


class TestInvalidArtifactValidation:
    def test_idea_task_missing_idea_section(self):
        tpl = build_idea_task_template()
        art = _make_artifact("/foo.md", ArtifactState.IdeaTask, tpl, [])
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("Required section 'Idea'" in e.message for e in report.errors)

    def test_idea_task_empty_idea_field(self):
        tpl = build_idea_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.IdeaTask,
            tpl,
            [Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("non-empty text" in e.message for e in report.errors)

    def test_idea_task_whitespace_idea_field(self):
        tpl = build_idea_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.IdeaTask,
            tpl,
            [Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="   ")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("non-empty text" in e.message for e in report.errors)

    def test_missing_required_field(self):
        tpl = build_defined_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.DefinedTask,
            tpl,
            [Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("missing" in e.message.lower() for e in report.errors)

    def test_field_type_mismatch(self):
        tpl = build_idea_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.IdeaTask,
            tpl,
            [Section(name="Idea", fields=[Field(name="Idea", type=FieldType.FILE, value="test")])],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("does not match template type" in e.message for e in report.errors)

    def test_optional_section_with_invalid_present_field(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[Field(name="Evidence", type=FieldType.STRING, value="")]),
            ],
        )
        report = validate_artifact(art)
        assert report.is_valid is False

    def test_defined_task_missing_direction_link(self):
        tpl = build_defined_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.DefinedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Technical direction", fields=[]),
            ],
        )
        report = validate_artifact(art)
        assert report.is_valid is False

    def test_defined_task_link_not_in_registry(self):
        tpl = build_defined_task_template()
        reg = ArtifactRegistry()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.DefinedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Technical direction", fields=[Field(name="Direction", type=FieldType.FILE, value="/nonexistent.md")]),
            ],
        )
        report = validate_artifact(art, reg)
        assert report.is_valid is False
        assert any("not found in registry" in e.message for e in report.errors)

    def test_defined_task_link_artifact_invalid(self):
        tpl = build_defined_task_template()
        reg = ArtifactRegistry()
        bad_tpl = build_technical_direction_template()
        bad_art = Artifact(path="/bad.md", state=ArtifactState.TechnicalDirection, template=bad_tpl, sections=[])
        reg.register(bad_art)
        art = _make_artifact(
            "/foo.md",
            ArtifactState.DefinedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Technical direction", fields=[Field(name="Direction", type=FieldType.FILE, value="/bad.md")]),
            ],
        )
        report = validate_artifact(art, reg)
        assert report.is_valid is False
        assert any("does not validate" in e.message for e in report.errors)

    def test_file_list_missing_registry_entry(self):
        tpl = build_completed_task_template()
        reg = ArtifactRegistry()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[
                    Field(name="Evidence", type=FieldType.STRING, value="done"),
                    Field(name="Changed files", type=FieldType.FILE_LIST, value=["/missing.md"]),
                    Field(name="Validation result", type=FieldType.STRING, value="ok"),
                ]),
            ],
        )
        report = validate_artifact(art, reg)
        assert report.is_valid is False
        assert any("not found in registry" in e.message for e in report.errors)

    def test_completed_task_done_no_proof_section(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
            ],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("must have a Proof section" in e.message for e in report.errors)

    def test_completed_task_done_empty_evidence(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[
                    Field(name="Evidence", type=FieldType.STRING, value=""),
                    Field(name="Changed files", type=FieldType.FILE_LIST, value=[]),
                    Field(name="Validation result", type=FieldType.STRING, value="ok"),
                ]),
            ],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("Evidence" in e.message for e in report.errors)

    def test_completed_task_done_empty_changed_files(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[
                    Field(name="Evidence", type=FieldType.STRING, value="done"),
                    Field(name="Changed files", type=FieldType.FILE_LIST, value=[]),
                    Field(name="Validation result", type=FieldType.STRING, value="ok"),
                ]),
            ],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("Changed files" in e.message for e in report.errors)

    def test_completed_task_done_empty_validation_result(self):
        tpl = build_completed_task_template()
        art = _make_artifact(
            "/foo.md",
            ArtifactState.CompletedTask,
            tpl,
            [
                Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")]),
                Section(name="Status", fields=[Field(name="Status", type=FieldType.STATUS, value="done")]),
                Section(name="Proof", fields=[
                    Field(name="Evidence", type=FieldType.STRING, value="done"),
                    Field(name="Changed files", type=FieldType.FILE_LIST, value=[]),
                    Field(name="Validation result", type=FieldType.STRING, value=""),
                ]),
            ],
        )
        report = validate_artifact(art)
        assert report.is_valid is False
        assert any("Validation result" in e.message for e in report.errors)
