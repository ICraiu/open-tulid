from __future__ import annotations

import pytest

from open_tulid.domain.schema import (
    ArtifactState,
    FieldTemplate,
    FieldType,
    SectionTemplate,
    Template,
    ValidatorType,
)
from open_tulid.domain.templates import (
    build_completed_task_template,
    build_defined_task_template,
    build_idea_task_template,
    build_technical_direction_template,
    build_technical_spec_template,
    get_builtin_templates,
)
from open_tulid.domain.validation import validate_template


class TestValidTemplates:
    def test_idea_task_template_valid(self):
        tpl = build_idea_task_template()
        report = validate_template(tpl)
        assert report.is_valid is True

    def test_technical_direction_template_valid(self):
        tpl = build_technical_direction_template()
        report = validate_template(tpl)
        assert report.is_valid is True

    def test_defined_task_template_valid(self):
        tpl = build_defined_task_template()
        report = validate_template(tpl)
        assert report.is_valid is True

    def test_completed_task_template_valid(self):
        tpl = build_completed_task_template()
        report = validate_template(tpl)
        assert report.is_valid is True

    def test_technical_spec_template_valid(self):
        tpl = build_technical_spec_template()
        report = validate_template(tpl)
        assert report.is_valid is True

    def test_get_builtin_templates(self):
        templates = get_builtin_templates()
        assert "IdeaTask" in templates
        assert "TechnicalDirection" in templates
        assert "DefinedTask" in templates
        assert "CompletedTask" in templates
        assert "TechnicalSpec" in templates

    def test_empty_template_sections_valid(self):
        tpl = Template(name="Empty", state=ArtifactState.IdeaTask)
        report = validate_template(tpl)
        assert report.is_valid is True


class TestInvalidTemplateName:
    def test_empty_name(self):
        tpl = Template(name="", state=ArtifactState.IdeaTask)
        report = validate_template(tpl)
        assert report.is_valid is False
        assert any("name" in e.location for e in report.errors)

    def test_whitespace_name(self):
        tpl = Template(name="   ", state=ArtifactState.IdeaTask)
        report = validate_template(tpl)
        assert report.is_valid is False


class TestDuplicateSections:
    def test_duplicate_section_names(self):
        tpl = Template(
            name="Dup",
            state=ArtifactState.IdeaTask,
            sections=[
                SectionTemplate(name="Idea"),
                SectionTemplate(name="Idea"),
            ],
        )
        report = validate_template(tpl)
        assert report.is_valid is False
        assert any("Duplicate section" in e.message for e in report.errors)


class TestInvalidSectionTemplate:
    def test_empty_section_name(self):
        tpl = Template(
            name="T",
            state=ArtifactState.IdeaTask,
            sections=[SectionTemplate(name="")],
        )
        report = validate_template(tpl)
        assert report.is_valid is False
        assert any("SectionTemplate name" in e.message for e in report.errors)

    def test_duplicate_field_names(self):
        from open_tulid.domain.schema import FieldTemplate

        tpl = Template(
            name="T",
            state=ArtifactState.IdeaTask,
            sections=[
                SectionTemplate(
                    name="Idea",
                    fields=[
                        FieldTemplate(name="Idea", type=FieldType.STRING),
                        FieldTemplate(name="Idea", type=FieldType.STRING),
                    ],
                )
            ],
        )
        report = validate_template(tpl)
        assert report.is_valid is False
        assert any("Duplicate field" in e.message for e in report.errors)


class TestInvalidFieldTemplate:
    def test_empty_field_name(self):
        from open_tulid.domain.schema import FieldTemplate

        tpl = Template(
            name="T",
            state=ArtifactState.IdeaTask,
            sections=[
                SectionTemplate(
                    name="Idea",
                    fields=[FieldTemplate(name="", type=FieldType.STRING)],
                )
            ],
        )
        report = validate_template(tpl)
        assert report.is_valid is False
        assert any("FieldTemplate name" in e.message for e in report.errors)

    def test_incompatible_validator(self):
        tpl = Template(
            name="T",
            state=ArtifactState.IdeaTask,
            sections=[
                SectionTemplate(
                    name="Idea",
                    fields=[
                        FieldTemplate(
                            name="Idea",
                            type=FieldType.STRING,
                            validators=[ValidatorType.FILE_LINK_EXISTS],
                        )
                    ],
                )
            ],
        )
        report = validate_template(tpl)
        assert report.is_valid is False
        assert any("not compatible" in e.message for e in report.errors)
