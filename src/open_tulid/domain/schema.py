from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ArtifactState(str, Enum):
    IdeaTask = "IdeaTask"
    DefinedTask = "DefinedTask"
    TechnicalDirection = "TechnicalDirection"
    TechnicalSpec = "TechnicalSpec"
    CompletedTask = "CompletedTask"


class FieldType(str, Enum):
    STRING = "STRING"
    FILE = "FILE"
    FILE_LIST = "FILE_LIST"
    STATUS = "STATUS"


class ValidatorType(str, Enum):
    NON_EMPTY_TEXT = "NON_EMPTY_TEXT"
    FILE_LINK_EXISTS = "FILE_LINK_EXISTS"
    FILE_LINK_MATCHES_TEMPLATE = "FILE_LINK_MATCHES_TEMPLATE"
    SECTION_PRESENT = "SECTION_PRESENT"
    REQUIRED_FIELD_PRESENT = "REQUIRED_FIELD_PRESENT"
    TASK_HAS_PROOF_WHEN_DONE = "TASK_HAS_PROOF_WHEN_DONE"


class MappingRuleType(str, Enum):
    CARRY_FIELD = "CARRY_FIELD"
    CREATE_SECTION = "CREATE_SECTION"
    SET_FIELD = "SET_FIELD"
    LINK_ARTIFACT = "LINK_ARTIFACT"


@dataclass
class ValidationError:
    path: str | None
    location: str
    message: str


@dataclass
class ValidationReport:
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, location: str, message: str, path: str | None = None) -> None:
        self.errors.append(ValidationError(path=path, location=location, message=message))


@dataclass
class RequiredWhen:
    field_name: str
    equals: str


@dataclass
class FieldTemplate:
    name: str
    type: FieldType
    required: bool = True
    required_when: RequiredWhen | None = None
    validators: list[ValidatorType] = field(default_factory=list)


@dataclass
class SectionTemplate:
    name: str
    fields: list[FieldTemplate] = field(default_factory=list)
    required: bool = True


@dataclass
class Template:
    name: str
    state: ArtifactState
    sections: list[SectionTemplate] = field(default_factory=list)


@dataclass
class Field:
    name: str
    type: FieldType
    value: str | list[str]


@dataclass
class Section:
    name: str
    fields: list[Field] = field(default_factory=list)


@dataclass
class Artifact:
    path: str
    state: ArtifactState
    template: Template
    sections: list[Section] = field(default_factory=list)
    source_artifacts: list[str] = field(default_factory=list)


@dataclass
class ArtifactRegistry:
    artifacts_by_path: dict[str, Artifact] = field(default_factory=dict)

    def register(self, artifact: Artifact) -> None:
        self.artifacts_by_path[artifact.path] = artifact

    def get(self, path: str) -> Artifact | None:
        return self.artifacts_by_path.get(path)

    def contains(self, path: str) -> bool:
        return path in self.artifacts_by_path


@dataclass
class MappingRule:
    kind: MappingRuleType
    from_section: str | None = None
    from_field: str | None = None
    to_section: str | None = None
    to_field: str | None = None
    value: str | None = None


@dataclass
class Transition:
    name: str
    from_state: ArtifactState
    to_state: ArtifactState
    required_inputs: list[Template] = field(default_factory=list)
    output_template: Template | None = None
    mapping_rules: list[MappingRule] = field(default_factory=list)
    validation_rules: list[ValidatorType] = field(default_factory=list)


@dataclass
class ArtifactReadResult:
    artifact: Artifact | None = None
    report: ValidationReport = field(default_factory=ValidationReport)


@dataclass
class ArtifactWriteResult:
    path: str | None = None
    content: str | None = None
    report: ValidationReport = field(default_factory=ValidationReport)


COMPATIBLE_VALIDATORS: dict[FieldType, set[ValidatorType]] = {
    FieldType.STRING: {ValidatorType.NON_EMPTY_TEXT, ValidatorType.REQUIRED_FIELD_PRESENT},
    FieldType.STATUS: {ValidatorType.NON_EMPTY_TEXT, ValidatorType.REQUIRED_FIELD_PRESENT},
    FieldType.FILE: {
        ValidatorType.FILE_LINK_EXISTS,
        ValidatorType.FILE_LINK_MATCHES_TEMPLATE,
        ValidatorType.REQUIRED_FIELD_PRESENT,
    },
    FieldType.FILE_LIST: {
        ValidatorType.FILE_LINK_EXISTS,
        ValidatorType.FILE_LINK_MATCHES_TEMPLATE,
        ValidatorType.REQUIRED_FIELD_PRESENT,
    },
}
