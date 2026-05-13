from __future__ import annotations

from .registry import (
    OperationSpec,
    RuntimeRegistries,
    ValidationSpec,
    WorkerSpec,
    build_registries,
)
from .implementations import OPERATION_IMPLEMENTATIONS, VALIDATION_IMPLEMENTATIONS

VALIDATION_IDS = [
    "project_build",
    "git_status_clean",
    "file_exists",
    "artifact_in_vault",
    "artifact_link_in_vault",
    "artifact_matches_template",
    "template_sections_present",
    "template_required_fields_present",
    "artifact_has_required_text",
    "branch_exists",
    "tests_pass",
    "link_target_exists",
]

OPERATION_IDS = [
    "move_task",
    "copy_file",
    "copy_field",
    "set_field",
    "link_artifact",
    "git_add",
    "git_commit",
    "git_reset_hard",
    "create_branch",
    "checkout_branch",
    "write_file",
    "append_event",
    "update_kanban_view",
]

WORKER_IDS = [
    "local_llm",
    "shell_command",
    "human_approval",
    "noop",
]

def _build_validations() -> list[ValidationSpec]:
    specs: list[ValidationSpec] = []
    for vid in VALIDATION_IDS:
        specs.append(ValidationSpec(id=vid, implementation=VALIDATION_IMPLEMENTATIONS[vid]))
    return specs


def _build_operations() -> list[OperationSpec]:
    specs: list[OperationSpec] = []
    for oid in OPERATION_IDS:
        kwargs: dict = {"id": oid, "implementation": OPERATION_IMPLEMENTATIONS[oid]}
        if oid == "git_reset_hard":
            kwargs["destructive"] = True
            kwargs["requires_approval"] = True
        specs.append(OperationSpec(**kwargs))
    return specs


def _build_workers() -> list[WorkerSpec]:
    specs: list[WorkerSpec] = []
    for wid in WORKER_IDS:
        specs.append(WorkerSpec(id=wid, implementation=wid))
    return specs


def get_builtin_registries() -> RuntimeRegistries:
    registries, diags = build_registries(
        validations=_build_validations(),
        operations=_build_operations(),
        workers=_build_workers(),
    )
    if registries is None:
        raise RuntimeError(f"built-in registries failed to build: {diags}")
    return registries
