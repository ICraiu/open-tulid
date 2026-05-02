from __future__ import annotations

from .schema import (
    ArtifactState,
    MappingRule,
    MappingRuleType,
    Transition,
    ValidatorType,
)
from .templates import (
    build_completed_task_template,
    build_defined_task_template,
    build_idea_task_template,
    build_technical_direction_template,
)


def build_define_task_transition() -> Transition:
    idea_tpl = build_idea_task_template()
    direction_tpl = build_technical_direction_template()
    output_tpl = build_defined_task_template()

    return Transition(
        name="DefineTask",
        from_state=ArtifactState.IdeaTask,
        to_state=ArtifactState.DefinedTask,
        required_inputs=[idea_tpl, direction_tpl],
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
                kind=MappingRuleType.LINK_ARTIFACT,
                from_section="Direction",
                from_field="Direction",
                to_section="Technical direction",
                to_field="Direction",
            ),
        ],
        validation_rules=[
            ValidatorType.NON_EMPTY_TEXT,
            ValidatorType.FILE_LINK_EXISTS,
            ValidatorType.FILE_LINK_MATCHES_TEMPLATE,
        ],
    )


def build_complete_task_transition() -> Transition:
    defined_tpl = build_defined_task_template()
    output_tpl = build_completed_task_template()

    return Transition(
        name="CompleteTask",
        from_state=ArtifactState.DefinedTask,
        to_state=ArtifactState.CompletedTask,
        required_inputs=[defined_tpl],
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
                to_section="Status",
                to_field="Status",
                value="done",
            ),
        ],
        validation_rules=[
            ValidatorType.TASK_HAS_PROOF_WHEN_DONE,
        ],
    )


def get_builtin_transitions() -> dict[str, Transition]:
    return {
        "DefineTask": build_define_task_transition(),
        "CompleteTask": build_complete_task_transition(),
    }
