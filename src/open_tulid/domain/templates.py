from __future__ import annotations

from .schema import (
    ArtifactState,
    FieldTemplate,
    FieldType,
    RequiredWhen,
    SectionTemplate,
    Template,
    ValidatorType,
)


def build_idea_task_template() -> Template:
    return Template(
        name="IdeaTask",
        state=ArtifactState.IdeaTask,
        sections=[
            SectionTemplate(
                name="Idea",
                required=True,
                fields=[
                    FieldTemplate(
                        name="Idea",
                        type=FieldType.STRING,
                        required=True,
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    )
                ],
            )
        ],
    )


def build_technical_direction_template() -> Template:
    return Template(
        name="TechnicalDirection",
        state=ArtifactState.TechnicalDirection,
        sections=[
            SectionTemplate(
                name="Direction",
                required=True,
                fields=[
                    FieldTemplate(
                        name="Direction",
                        type=FieldType.STRING,
                        required=True,
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    )
                ],
            )
        ],
    )


def build_defined_task_template() -> Template:
    return Template(
        name="DefinedTask",
        state=ArtifactState.DefinedTask,
        sections=[
            SectionTemplate(
                name="Idea",
                required=True,
                fields=[
                    FieldTemplate(
                        name="Idea",
                        type=FieldType.STRING,
                        required=True,
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    )
                ],
            ),
            SectionTemplate(
                name="Technical direction",
                required=True,
                fields=[
                    FieldTemplate(
                        name="Direction",
                        type=FieldType.FILE,
                        required=True,
                        validators=[
                            ValidatorType.FILE_LINK_EXISTS,
                            ValidatorType.FILE_LINK_MATCHES_TEMPLATE,
                        ],
                    )
                ],
            ),
        ],
    )


def build_completed_task_template() -> Template:
    return Template(
        name="CompletedTask",
        state=ArtifactState.CompletedTask,
        sections=[
            SectionTemplate(
                name="Idea",
                required=True,
                fields=[
                    FieldTemplate(
                        name="Idea",
                        type=FieldType.STRING,
                        required=True,
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    )
                ],
            ),
            SectionTemplate(
                name="Status",
                required=True,
                fields=[
                    FieldTemplate(
                        name="Status",
                        type=FieldType.STATUS,
                        required=True,
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    )
                ],
            ),
            SectionTemplate(
                name="Proof",
                required=False,
                fields=[
                    FieldTemplate(
                        name="Evidence",
                        type=FieldType.STRING,
                        required=False,
                        required_when=RequiredWhen(field_name="Status", equals="done"),
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    ),
                    FieldTemplate(
                        name="Changed files",
                        type=FieldType.FILE_LIST,
                        required=False,
                        required_when=RequiredWhen(field_name="Status", equals="done"),
                        validators=[ValidatorType.FILE_LINK_EXISTS],
                    ),
                    FieldTemplate(
                        name="Validation result",
                        type=FieldType.STRING,
                        required=False,
                        required_when=RequiredWhen(field_name="Status", equals="done"),
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    ),
                ],
            ),
        ],
    )


def build_technical_spec_template() -> Template:
    return Template(
        name="TechnicalSpec",
        state=ArtifactState.TechnicalSpec,
        sections=[
            SectionTemplate(
                name="Overview",
                required=True,
                fields=[
                    FieldTemplate(
                        name="Overview",
                        type=FieldType.STRING,
                        required=True,
                        validators=[ValidatorType.NON_EMPTY_TEXT],
                    )
                ],
            )
        ],
    )


def get_builtin_templates() -> dict[str, Template]:
    return {
        "IdeaTask": build_idea_task_template(),
        "TechnicalDirection": build_technical_direction_template(),
        "DefinedTask": build_defined_task_template(),
        "CompletedTask": build_completed_task_template(),
        "TechnicalSpec": build_technical_spec_template(),
    }

