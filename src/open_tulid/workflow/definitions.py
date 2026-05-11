from __future__ import annotations

import dataclasses
from typing import Mapping


@dataclasses.dataclass(frozen=True)
class ArgDefinition:
    type: str
    required: bool = False
    many: bool = False


@dataclasses.dataclass(frozen=True)
class WorkflowDefinition:
    schema_version: int
    states: Mapping[str, "StateDefinition"]
    task_types: Mapping[str, "TaskTypeDefinition"]
    artifact_types: Mapping[str, "ArtifactTypeDefinition"]
    validation_types: Mapping[str, "ValidationTypeDefinition"]
    operation_types: Mapping[str, "OperationTypeDefinition"]
    workers: Mapping[str, "WorkerDefinition"]
    transitions: Mapping[str, "TransitionDefinition"]


@dataclasses.dataclass(frozen=True)
class StateDefinition:
    id: str


@dataclasses.dataclass(frozen=True)
class TaskTypeDefinition:
    id: str
    requirements_by_state: Mapping[str, "RequirementDefinition"]


@dataclasses.dataclass(frozen=True)
class ArtifactTypeDefinition:
    id: str
    template: str | None = None
    handler: str | None = None


@dataclasses.dataclass(frozen=True)
class ValidationTypeDefinition:
    id: str
    args: Mapping[str, ArgDefinition]
    implementation_id: str


@dataclasses.dataclass(frozen=True)
class OperationTypeDefinition:
    id: str
    args: Mapping[str, ArgDefinition]
    implementation_id: str


@dataclasses.dataclass(frozen=True)
class WorkerDefinition:
    id: str
    type: str | None = None
    implementation_id: str | None = None


@dataclasses.dataclass(frozen=True)
class TransitionDefinition:
    id: str
    task_type: str
    from_state: str
    to_state: str
    worker: str | None
    requires: "RequirementDefinition"
    transaction: "TransactionDefinition" | None


@dataclasses.dataclass(frozen=True)
class RequirementDefinition:
    artifacts: tuple[str, ...] = ()
    validations: tuple["ValidationCallDefinition", ...] = ()


@dataclasses.dataclass(frozen=True)
class ValidationCallDefinition:
    type: str
    args: Mapping[str, object]


@dataclasses.dataclass(frozen=True)
class OperationCallDefinition:
    op: str
    args: Mapping[str, object]


@dataclasses.dataclass(frozen=True)
class TransactionDefinition:
    steps: tuple["OperationCallDefinition", ...]
