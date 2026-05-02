from __future__ import annotations

import pytest

from open_tulid.domain.schema import (
    ArtifactState,
    FieldTemplate,
    FieldType,
    MappingRule,
    MappingRuleType,
    SectionTemplate,
    Template,
    Transition,
    ValidatorType,
)
from open_tulid.domain.templates import (
    build_completed_task_template,
    build_defined_task_template,
    build_idea_task_template,
    build_technical_direction_template,
)
from open_tulid.domain.transitions import (
    build_complete_task_transition,
    build_define_task_transition,
    get_builtin_transitions,
)
from open_tulid.domain.validation import validate_transition


class TestValidTransitions:
    def test_define_task_transition_valid(self):
        t = build_define_task_transition()
        report = validate_transition(t)
        assert report.is_valid is True

    def test_complete_task_transition_valid(self):
        t = build_complete_task_transition()
        report = validate_transition(t)
        assert report.is_valid is True

    def test_get_builtin_transitions(self):
        transitions = get_builtin_transitions()
        assert "DefineTask" in transitions
        assert "CompleteTask" in transitions

    def test_transition_with_one_output_template(self):
        idea_tpl = build_idea_task_template()
        output_tpl = build_defined_task_template()
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[idea_tpl],
            output_template=output_tpl,
            mapping_rules=[
                MappingRule(
                    kind=MappingRuleType.CARRY_FIELD,
                    from_section="Idea",
                    from_field="Idea",
                    to_section="Idea",
                    to_field="Idea",
                ),
                MappingRule(
                    kind=MappingRuleType.SET_FIELD,
                    to_section="Technical direction",
                    to_field="Direction",
                    value="direction.md",
                ),
            ],
        )
        report = validate_transition(t)
        assert report.is_valid is True


class TestInvalidTransitionSchema:
    def test_empty_name(self):
        t = Transition(name="", from_state=ArtifactState.IdeaTask, to_state=ArtifactState.DefinedTask)
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("name" in e.location for e in report.errors)

    def test_same_from_and_to_state(self):
        t = Transition(name="Test", from_state=ArtifactState.IdeaTask, to_state=ArtifactState.IdeaTask)
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("different" in e.message for e in report.errors)

    def test_empty_required_inputs(self):
        t = Transition(name="Test", from_state=ArtifactState.IdeaTask, to_state=ArtifactState.DefinedTask)
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("required input" in e.message for e in report.errors)

    def test_no_required_input_matching_from_state(self):
        direction_tpl = build_technical_direction_template()
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[direction_tpl],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("from_state" in e.message for e in report.errors)

    def test_output_template_state_mismatch(self):
        idea_tpl = build_idea_task_template()
        wrong_tpl = Template(name="Wrong", state=ArtifactState.TechnicalSpec)
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[idea_tpl],
            output_template=wrong_tpl,
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("output_template state" in e.message for e in report.errors)

    def test_missing_output_template(self):
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[build_idea_task_template()],
            output_template=None,
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("output_template" in e.location for e in report.errors)

    def test_carry_field_missing_source(self):
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[build_idea_task_template()],
            output_template=build_defined_task_template(),
            mapping_rules=[
                MappingRule(kind=MappingRuleType.CARRY_FIELD, to_section="Idea", to_field="Idea"),
            ],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("CARRY_FIELD" in e.message for e in report.errors)

    def test_carry_field_missing_target(self):
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[build_idea_task_template()],
            output_template=build_defined_task_template(),
            mapping_rules=[
                MappingRule(kind=MappingRuleType.CARRY_FIELD, from_section="Idea", from_field="Idea"),
            ],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("CARRY_FIELD" in e.message for e in report.errors)

    def test_set_field_missing_target(self):
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[build_idea_task_template()],
            output_template=build_defined_task_template(),
            mapping_rules=[
                MappingRule(kind=MappingRuleType.SET_FIELD, value="done"),
            ],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("SET_FIELD" in e.message for e in report.errors)

    def test_set_field_missing_value(self):
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[build_idea_task_template()],
            output_template=build_defined_task_template(),
            mapping_rules=[
                MappingRule(kind=MappingRuleType.SET_FIELD, to_section="Status", to_field="Status"),
            ],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("SET_FIELD" in e.message for e in report.errors)

    def test_empty_mapping_rules_fail_when_output_requires_content(self):
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[build_idea_task_template()],
            output_template=build_defined_task_template(),
            mapping_rules=[],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("mapping_rules" in e.location for e in report.errors)

    def test_create_section_missing_target(self):
        t = Transition(
            name="Test",
            from_state=ArtifactState.IdeaTask,
            to_state=ArtifactState.DefinedTask,
            required_inputs=[build_idea_task_template()],
            output_template=build_defined_task_template(),
            mapping_rules=[
                MappingRule(kind=MappingRuleType.CREATE_SECTION),
            ],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("CREATE_SECTION" in e.message for e in report.errors)

    def test_link_artifact_target_non_file_field(self):
        output_tpl = Template(
            name="TestOutput",
            state=ArtifactState.IdeaTask,
            sections=[
                SectionTemplate(
                    name="Idea",
                    fields=[
                        FieldTemplate(name="Idea", type=FieldType.STRING, required=True),
                    ],
                ),
            ],
        )
        t = Transition(
            name="Test",
            from_state=ArtifactState.TechnicalDirection,
            to_state=ArtifactState.IdeaTask,
            required_inputs=[build_technical_direction_template()],
            output_template=output_tpl,
            mapping_rules=[
                MappingRule(
                    kind=MappingRuleType.LINK_ARTIFACT,
                    from_section="Direction",
                    from_field="Direction",
                    to_section="Idea",
                    to_field="Idea",
                ),
            ],
        )
        report = validate_transition(t)
        assert report.is_valid is False
        assert any("non-file field" in e.message for e in report.errors)
