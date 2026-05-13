from __future__ import annotations

import dataclasses
from typing import Iterable, Mapping
from types import MappingProxyType

from workflow_engine import langdef

from open_tulid.domain.schema import ArgDefinition
from .diagnostics import WorkflowCompileDiagnostic


@dataclasses.dataclass(frozen=True)
class ValidationSpec:
    id: str
    implementation: object
    args: Mapping[str, ArgDefinition] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class OperationSpec:
    id: str
    implementation: object
    args: Mapping[str, ArgDefinition] = dataclasses.field(default_factory=dict)
    destructive: bool = False
    requires_approval: bool = False
    cleanup_operation: str | None = None


@dataclasses.dataclass(frozen=True)
class WorkerSpec:
    id: str
    implementation: object


@dataclasses.dataclass(frozen=True)
class RuntimeRegistries:
    validations: Mapping[str, ValidationSpec]
    operations: Mapping[str, OperationSpec]
    workers: Mapping[str, WorkerSpec]


def _freeze_mapping(mapping: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(mapping))


# --- Shared validation helpers ---

def _check_empty_id(diagnostics: list[WorkflowCompileDiagnostic], spec_id: str, kind: str) -> bool:
    if not spec_id:
        diagnostics.append(WorkflowCompileDiagnostic(
            code="workflow.compile.registry_duplicate_id",
            message=f"{kind} registry entry has empty id",
        ))
        return True
    return False


def _check_missing_impl(diagnostics: list[WorkflowCompileDiagnostic], spec_id: str, impl: object, kind: str) -> bool:
    if impl is None:
        diagnostics.append(WorkflowCompileDiagnostic(
            code="workflow.compile.registry_missing_implementation",
            message=f"{kind} {spec_id!r} has no implementation",
        ))
        return True
    return False


def _check_arg_types(
    diagnostics: list[WorkflowCompileDiagnostic],
    spec_id: str,
    args: Mapping[str, ArgDefinition],
    kind: str,
) -> None:
    for arg_name, arg_def in args.items():
        if arg_def.type not in langdef.SUPPORTED_ARG_TYPES:
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.registry_invalid_argument_type",
                message=f"{kind} {spec_id!r} arg {arg_name!r} has unsupported type: {arg_def.type!r}",
            ))


def _check_key_id_mismatch(
    diagnostics: list[WorkflowCompileDiagnostic],
    key: str,
    spec_id: str,
    kind: str,
) -> bool:
    if key != spec_id:
        diagnostics.append(WorkflowCompileDiagnostic(
            code="workflow.compile.registry_duplicate_id",
            message=f"{kind} registry key {key!r} does not match spec id {spec_id!r}",
        ))
        return True
    return False


def _check_duplicate_logical_ids(
    diagnostics: list[WorkflowCompileDiagnostic],
    mapping: Mapping[str, object],
    kind: str,
) -> None:
    seen: dict[str, str] = {}
    for key, spec in mapping.items():
        spec_id: str = spec.id  # type: ignore[attr-defined]
        if spec_id in seen:
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.registry_duplicate_id",
                message=f"duplicate {kind} registry id {spec_id!r} (keys {seen[spec_id]!r} and {key!r})",
            ))
        else:
            seen[spec_id] = key


def _validate_validation_spec(
    diagnostics: list[WorkflowCompileDiagnostic],
    spec_id: str,
    spec: ValidationSpec,
) -> None:
    if _check_empty_id(diagnostics, spec_id, "validation"):
        return
    if _check_missing_impl(diagnostics, spec_id, spec.implementation, "validation"):
        return
    _check_arg_types(diagnostics, spec_id, spec.args, "validation")


def _validate_operation_spec(
    diagnostics: list[WorkflowCompileDiagnostic],
    spec_id: str,
    spec: OperationSpec,
    op_map: Mapping[str, OperationSpec] | None = None,
) -> None:
    if _check_empty_id(diagnostics, spec_id, "operation"):
        return
    if _check_missing_impl(diagnostics, spec_id, spec.implementation, "operation"):
        return
    _check_arg_types(diagnostics, spec_id, spec.args, "operation")
    if spec.cleanup_operation is not None and op_map is not None:
        if spec.cleanup_operation not in op_map:
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.registry_missing_implementation",
                message=f"operation {spec_id!r} cleanup_operation {spec.cleanup_operation!r} not found in registry",
            ))


def _validate_simple_spec(
    diagnostics: list[WorkflowCompileDiagnostic],
    spec_id: str,
    impl: object,
    kind: str,
) -> None:
    if _check_empty_id(diagnostics, spec_id, kind):
        return
    _check_missing_impl(diagnostics, spec_id, impl, kind)


# --- Registry builders ---

def _build_registry_map(
    specs: Iterable[object],
    kind: str,
    map_var: dict[str, object],
    diagnostics: list[WorkflowCompileDiagnostic],
) -> None:
    for spec in specs:
        spec_id: str = spec.id  # type: ignore[attr-defined]
        if spec_id in map_var:
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.registry_duplicate_id",
                message=f"duplicate {kind} registry id: {spec_id!r}",
            ))
        elif not spec_id:
            diagnostics.append(WorkflowCompileDiagnostic(
                code="workflow.compile.registry_duplicate_id",
                message=f"{kind} registry entry has empty id",
            ))
        else:
            map_var[spec_id] = spec


def build_registries(
    *,
    validations: Iterable[ValidationSpec] = (),
    operations: Iterable[OperationSpec] = (),
    workers: Iterable[WorkerSpec] = (),
) -> tuple[RuntimeRegistries | None, tuple[WorkflowCompileDiagnostic, ...]]:
    diagnostics: list[WorkflowCompileDiagnostic] = []

    # Phase 1: duplicate + empty-id checks (unique to build path)
    val_map: dict[str, ValidationSpec] = {}
    _build_registry_map(validations, "validation", val_map, diagnostics)

    op_map: dict[str, OperationSpec] = {}
    _build_registry_map(operations, "operation", op_map, diagnostics)

    worker_map: dict[str, WorkerSpec] = {}
    _build_registry_map(workers, "worker", worker_map, diagnostics)

    # Phase 2: implementation + arg-type checks (shared with validate_registries)
    for spec_id, spec in val_map.items():
        _validate_validation_spec(diagnostics, spec_id, spec)

    for spec_id, spec in op_map.items():
        _validate_operation_spec(diagnostics, spec_id, spec, op_map)

    for spec_id, spec in worker_map.items():
        _validate_simple_spec(diagnostics, spec_id, spec.implementation, "worker")

    if diagnostics:
        return None, tuple(diagnostics)

    return RuntimeRegistries(
        validations=_freeze_mapping(val_map),
        operations=_freeze_mapping(op_map),
        workers=_freeze_mapping(worker_map),
    ), ()


def validate_registries(registries: RuntimeRegistries) -> tuple[WorkflowCompileDiagnostic, ...]:
    diagnostics: list[WorkflowCompileDiagnostic] = []

    _check_duplicate_logical_ids(diagnostics, registries.validations, "validation")
    for key, spec in registries.validations.items():
        if _check_key_id_mismatch(diagnostics, key, spec.id, "validation"):
            continue
        _validate_validation_spec(diagnostics, key, spec)

    _check_duplicate_logical_ids(diagnostics, registries.operations, "operation")
    for key, spec in registries.operations.items():
        if _check_key_id_mismatch(diagnostics, key, spec.id, "operation"):
            continue
        _validate_operation_spec(diagnostics, key, spec, registries.operations)

    _check_duplicate_logical_ids(diagnostics, registries.workers, "worker")
    for key, spec in registries.workers.items():
        if _check_key_id_mismatch(diagnostics, key, spec.id, "worker"):
            continue
        _validate_simple_spec(diagnostics, key, spec.implementation, "worker")

    return tuple(diagnostics)
