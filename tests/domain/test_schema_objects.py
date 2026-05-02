from __future__ import annotations

import pytest

from open_tulid.domain.schema import (
    Artifact,
    ArtifactReadResult,
    ArtifactRegistry,
    ArtifactState,
    ArtifactWriteResult,
    Field,
    FieldTemplate,
    FieldType,
    MappingRule,
    MappingRuleType,
    RequiredWhen,
    Section,
    SectionTemplate,
    Template,
    Transition,
    ValidationReport,
    ValidationError,
    ValidatorType,
)


class TestArtifactState:
    def test_idea_task_state(self):
        assert ArtifactState.IdeaTask.value == "IdeaTask"

    def test_defined_task_state(self):
        assert ArtifactState.DefinedTask.value == "DefinedTask"

    def test_technical_direction_state(self):
        assert ArtifactState.TechnicalDirection.value == "TechnicalDirection"

    def test_technical_spec_state(self):
        assert ArtifactState.TechnicalSpec.value == "TechnicalSpec"

    def test_completed_task_state(self):
        assert ArtifactState.CompletedTask.value == "CompletedTask"


class TestFieldType:
    def test_string_type(self):
        assert FieldType.STRING.value == "STRING"

    def test_file_type(self):
        assert FieldType.FILE.value == "FILE"

    def test_file_list_type(self):
        assert FieldType.FILE_LIST.value == "FILE_LIST"

    def test_status_type(self):
        assert FieldType.STATUS.value == "STATUS"


class TestValidatorType:
    def test_non_empty_text(self):
        assert ValidatorType.NON_EMPTY_TEXT.value == "NON_EMPTY_TEXT"

    def test_file_link_exists(self):
        assert ValidatorType.FILE_LINK_EXISTS.value == "FILE_LINK_EXISTS"

    def test_file_link_matches_template(self):
        assert ValidatorType.FILE_LINK_MATCHES_TEMPLATE.value == "FILE_LINK_MATCHES_TEMPLATE"

    def test_section_present(self):
        assert ValidatorType.SECTION_PRESENT.value == "SECTION_PRESENT"

    def test_required_field_present(self):
        assert ValidatorType.REQUIRED_FIELD_PRESENT.value == "REQUIRED_FIELD_PRESENT"

    def test_task_has_proof_when_done(self):
        assert ValidatorType.TASK_HAS_PROOF_WHEN_DONE.value == "TASK_HAS_PROOF_WHEN_DONE"


class TestMappingRuleType:
    def test_carry_field(self):
        assert MappingRuleType.CARRY_FIELD.value == "CARRY_FIELD"

    def test_create_section(self):
        assert MappingRuleType.CREATE_SECTION.value == "CREATE_SECTION"

    def test_set_field(self):
        assert MappingRuleType.SET_FIELD.value == "SET_FIELD"

    def test_link_artifact(self):
        assert MappingRuleType.LINK_ARTIFACT.value == "LINK_ARTIFACT"


class TestValidationReport:
    def test_valid_report(self):
        report = ValidationReport()
        assert report.is_valid is True

    def test_invalid_report(self):
        report = ValidationReport()
        report.add_error("test", "something failed")
        assert report.is_valid is False

    def test_report_contains_errors(self):
        report = ValidationReport()
        report.add_error("loc", "msg")
        assert len(report.errors) == 1
        assert report.errors[0].location == "loc"
        assert report.errors[0].message == "msg"

    def test_validation_error_fields(self):
        err = ValidationError(path="p", location="l", message="m")
        assert err.path == "p"
        assert err.location == "l"
        assert err.message == "m"


class TestArtifactRegistry:
    def test_empty_registry(self):
        reg = ArtifactRegistry()
        assert reg.artifacts_by_path == {}
        assert reg.contains("foo") is False
        assert reg.get("foo") is None

    def test_register_and_get(self):
        reg = ArtifactRegistry()
        tpl = Template(name="T", state=ArtifactState.IdeaTask)
        art = Artifact(path="/foo.md", state=ArtifactState.IdeaTask, template=tpl)
        reg.register(art)
        assert reg.contains("/foo.md") is True
        assert reg.get("/foo.md") is art

    def test_register_overwrites(self):
        reg = ArtifactRegistry()
        tpl = Template(name="T", state=ArtifactState.IdeaTask)
        art1 = Artifact(path="/foo.md", state=ArtifactState.IdeaTask, template=tpl)
        art2 = Artifact(path="/foo.md", state=ArtifactState.DefinedTask, template=tpl)
        reg.register(art1)
        reg.register(art2)
        assert reg.get("/foo.md") is art2


class TestReadResult:
    def test_success_result(self):
        tpl = Template(name="T", state=ArtifactState.IdeaTask)
        art = Artifact(path="/foo.md", state=ArtifactState.IdeaTask, template=tpl)
        result = ArtifactReadResult(artifact=art)
        assert result.artifact is art
        assert result.report.is_valid is True

    def test_failure_result(self):
        result = ArtifactReadResult()
        assert result.artifact is None
        assert result.report.is_valid is True

    def test_failure_with_errors(self):
        result = ArtifactReadResult()
        result.report.add_error("x", "y")
        assert result.artifact is None
        assert result.report.is_valid is False


class TestWriteResult:
    def test_success_result(self):
        result = ArtifactWriteResult(path="/out.md", content="# Hello")
        assert result.path == "/out.md"
        assert result.content == "# Hello"
        assert result.report.is_valid is True

    def test_failure_result(self):
        result = ArtifactWriteResult()
        assert result.path is None
        assert result.content is None


class TestRequiredWhen:
    def test_required_when_fields(self):
        rw = RequiredWhen(field_name="Status", equals="done")
        assert rw.field_name == "Status"
        assert rw.equals == "done"


class TestFieldTemplate:
    def test_minimal_field_template(self):
        ft = FieldTemplate(name="Idea", type=FieldType.STRING)
        assert ft.name == "Idea"
        assert ft.type == FieldType.STRING
        assert ft.required is True
        assert ft.required_when is None
        assert ft.validators == []


class TestSectionTemplate:
    def test_minimal_section_template(self):
        st = SectionTemplate(name="Idea")
        assert st.name == "Idea"
        assert st.required is True
        assert st.fields == []


class TestTemplate:
    def test_minimal_template(self):
        tpl = Template(name="IdeaTask", state=ArtifactState.IdeaTask)
        assert tpl.name == "IdeaTask"
        assert tpl.state == ArtifactState.IdeaTask
        assert tpl.sections == []


class TestField:
    def test_string_field(self):
        f = Field(name="Idea", type=FieldType.STRING, value="test")
        assert f.name == "Idea"
        assert f.type == FieldType.STRING
        assert f.value == "test"

    def test_file_list_field(self):
        f = Field(name="Files", type=FieldType.FILE_LIST, value=["a.md", "b.md"])
        assert f.value == ["a.md", "b.md"]


class TestSection:
    def test_section_with_fields(self):
        sec = Section(name="Idea", fields=[Field(name="Idea", type=FieldType.STRING, value="test")])
        assert sec.name == "Idea"
        assert len(sec.fields) == 1


class TestArtifact:
    def test_minimal_artifact(self):
        tpl = Template(name="IdeaTask", state=ArtifactState.IdeaTask)
        art = Artifact(path="/foo.md", state=ArtifactState.IdeaTask, template=tpl)
        assert art.path == "/foo.md"
        assert art.state == ArtifactState.IdeaTask
        assert art.sections == []
        assert art.source_artifacts == []


class TestTransition:
    def test_minimal_transition(self):
        t = Transition(name="Test", from_state=ArtifactState.IdeaTask, to_state=ArtifactState.DefinedTask)
        assert t.name == "Test"
        assert t.required_inputs == []
        assert t.output_template is None
        assert t.mapping_rules == []
        assert t.validation_rules == []


class TestMappingRule:
    def test_carry_field_rule(self):
        mr = MappingRule(
            kind=MappingRuleType.CARRY_FIELD,
            from_section="Idea",
            from_field="Idea",
            to_section="Idea",
            to_field="Idea",
        )
        assert mr.kind == MappingRuleType.CARRY_FIELD

    def test_set_field_rule(self):
        mr = MappingRule(
            kind=MappingRuleType.SET_FIELD,
            to_section="Status",
            to_field="Status",
            value="done",
        )
        assert mr.kind == MappingRuleType.SET_FIELD
        assert mr.value == "done"

    def test_link_artifact_rule(self):
        mr = MappingRule(
            kind=MappingRuleType.LINK_ARTIFACT,
            from_section="Direction",
            from_field="Direction",
            to_section="Technical direction",
            to_field="Direction",
        )
        assert mr.kind == MappingRuleType.LINK_ARTIFACT

    def test_create_section_rule(self):
        mr = MappingRule(
            kind=MappingRuleType.CREATE_SECTION,
            to_section="Proof",
        )
        assert mr.kind == MappingRuleType.CREATE_SECTION
