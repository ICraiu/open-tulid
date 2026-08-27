from __future__ import annotations

from typing import Final

SUPPORTED_KINDS: Final = frozenset({
    "state",
    "task_type",
    "artifact_type",
    "validation_type",
    "worker",
    "operation_type",
    "transition",
})

STATEMENT_KEYS: Final = {
    "state": frozenset({"kind", "id"}),
    "task_type": frozenset({"kind", "id", "requirements", "instructions"}),
    "artifact_type": frozenset({"kind", "id", "template"}),
    "validation_type": frozenset({"kind", "id", "args"}),
    "worker": frozenset({"kind", "id", "type", "instructions"}),
    "operation_type": frozenset({"kind", "id", "args"}),
    "transition": frozenset({
        "kind", "id", "task_type", "from", "to", "worker", "default_for_scheduler",
        "requires", "transaction", "instructions", "derives",
    }),
}

STATEMENT_REQUIRED_KEYS: Final = {
    "state": frozenset({"kind", "id"}),
    "task_type": frozenset({"kind", "id"}),
    "artifact_type": frozenset({"kind", "id"}),
    "validation_type": frozenset({"kind", "id"}),
    "worker": frozenset({"kind", "id"}),
    "operation_type": frozenset({"kind", "id"}),
    "transition": frozenset({"kind", "id", "task_type", "from", "to"}),
}

# Backwards compat alias
TRANSITION_REQUIRED_KEYS: Final = STATEMENT_REQUIRED_KEYS["transition"]

SUPPORTED_ARG_TYPES: Final = frozenset({
    "string",
    "integer",
    "boolean",
    "state_ref",
    "task_type_ref",
    "artifact_ref",
    "validation_ref",
    "worker_ref",
    "operation_ref",
})

REFERENCE_ARG_TYPES: Final = {
    "state_ref": "state",
    "task_type_ref": "task_type",
    "artifact_ref": "artifact_type",
    "validation_ref": "validation_type",
    "worker_ref": "worker",
    "operation_ref": "operation_type",
}

ARG_SPEC_KEYS: Final = frozenset({"type", "required", "many"})

REQUIREMENT_SET_KEYS: Final = frozenset({"artifacts", "validations", "changed_files"})

VALIDATION_CALL_KEYS: Final = frozenset({"type", "args"})

OPERATION_CALL_KEYS: Final = frozenset({"op", "args"})

TRANSACTION_PLAN_KEYS: Final = frozenset({"steps"})
DERIVES_KEYS: Final = frozenset({
    "task_type", "state", "artifact_type", "required", "parent_to_if_derived",
})
DERIVES_REQUIRED_KEYS: Final = frozenset({"task_type", "state", "artifact_type"})

SUPPORTED_SCHEMA_VERSIONS: Final = frozenset({1})
