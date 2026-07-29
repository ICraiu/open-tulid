from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Mapping, Sequence

from ruamel.yaml import YAML

from open_tulid.domain import DomainError, Task, WorkflowDefinition


IMPLEMENTATION_CONTRACT_SCHEMA = "tulid.implementation/v1"
IMPLEMENTATION_CONTRACT_PROFILES = frozenset({
    "bootstrap",
    "bug_fix",
    "code_change",
    "configuration",
    "documentation",
    "integration",
    "refactor",
    "test_only",
})
CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHELL_CONTROL_TOKENS = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "<<"})


@dataclass(frozen=True)
class ChangeSurface:
    add: tuple[str, ...] = ()
    edit: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    max_files: int | None = None
    max_changed_lines: int | None = None


@dataclass(frozen=True)
class CheckExpectation:
    exit_code: int = 0
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractCheck:
    id: str
    argv: tuple[str, ...]
    timeout_seconds: int
    expect: CheckExpectation


@dataclass(frozen=True)
class ContractInterface:
    symbol: str
    behavior: str
    signature: str | None = None


@dataclass(frozen=True)
class ContextExcerpt:
    artifact: str
    heading: str


@dataclass(frozen=True)
class ImplementationContractDraft:
    schema: str
    source_task_id: str
    source_intent_sha256: str
    profile: str
    objective: str
    change_surface: ChangeSurface
    requirements: tuple[str, ...]
    focused_checks: tuple[ContractCheck, ...]
    invariants: tuple[str, ...]
    acceptance_profiles: tuple[str, ...] = ()
    interfaces: tuple[ContractInterface, ...] = ()
    failure_behavior: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    context_excerpts: tuple[ContextExcerpt, ...] = ()


@dataclass(frozen=True)
class ContractParseResult:
    contract: ImplementationContractDraft | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


def task_source_intent_sha256(task: Task) -> str:
    """Hash user-owned task intent while excluding workflow state and artifact links."""
    payload = {
        "id": task.id,
        "title": task.title,
        "path": task.path,
        "task_type": task.task_type,
        "dependencies": list(task.dependencies),
        "parent_id": task.parent_id,
        "metadata": dict(task.metadata),
        "body": task.body,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def implementation_contract_required(
    task: Task,
    workflow: WorkflowDefinition,
) -> bool:
    task_type = workflow.task_types.get(task.task_type)
    if task_type is None:
        return False
    requirements = task_type.requirements_by_state.get(task.current_state)
    return (
        requirements is not None
        and "ImplementationContract" in requirements.artifacts
    )


def parse_implementation_contract_file(
    path: Path,
    *,
    expected_task_id: str | None = None,
    expected_source_intent_sha256: str | None = None,
) -> ContractParseResult:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ContractParseResult(errors=(_error(
            "contract.read_failed",
            f"Cannot read implementation contract: {exc}",
            str(path),
        ),))
    return parse_implementation_contract(
        text,
        expected_task_id=expected_task_id,
        expected_source_intent_sha256=expected_source_intent_sha256,
        location=str(path),
    )


def find_implementation_contract_path(
    project_root: Path,
    task: Task,
) -> Path | None:
    root = project_root.resolve()
    for link in reversed(task.artifact_links):
        clean = link.strip()
        if clean.startswith("[[") and clean.endswith("]]"):
            clean = clean[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
        relative = Path(clean)
        if (
            "ImplementationContract" not in relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            continue
        candidate = (root / relative).resolve()
        if root != candidate and root not in candidate.parents:
            continue
        if candidate.is_file():
            return candidate
    return None


def validate_task_implementation_contract(
    project_root: Path,
    task: Task,
) -> ContractParseResult:
    path = find_implementation_contract_path(project_root, task)
    if path is None:
        return ContractParseResult(errors=(_error(
            "contract.artifact_missing",
            f"Task {task.id!r} has no readable linked ImplementationContract artifact.",
            task.id,
        ),))
    return parse_implementation_contract_file(
        path,
        expected_task_id=task.id,
        expected_source_intent_sha256=task_source_intent_sha256(task),
    )


def parse_implementation_contract(
    text: str,
    *,
    expected_task_id: str | None = None,
    expected_source_intent_sha256: str | None = None,
    location: str = "implementation-contract.yaml",
) -> ContractParseResult:
    try:
        raw = YAML(typ="safe").load(StringIO(text))
    except Exception as exc:
        return ContractParseResult(errors=(_error(
            "contract.invalid_yaml",
            f"Implementation contract is not valid YAML: {exc}",
            location,
        ),))
    if not isinstance(raw, Mapping):
        return ContractParseResult(errors=(_error(
            "contract.invalid_shape",
            "Implementation contract must be a YAML mapping.",
            location,
        ),))

    errors: list[DomainError] = []
    allowed_top_level = {
        "schema",
        "source",
        "profile",
        "objective",
        "change_surface",
        "interfaces",
        "requirements",
        "failure_behavior",
        "non_goals",
        "checks",
        "context_excerpts",
    }
    _reject_unknown_fields(raw, allowed_top_level, errors, location)

    schema = _required_string(raw, "schema", errors, location)
    if schema and schema != IMPLEMENTATION_CONTRACT_SCHEMA:
        errors.append(_error(
            "contract.unsupported_schema",
            f"Unsupported implementation contract schema: {schema}",
            f"{location}.schema",
        ))

    source = raw.get("source")
    if not isinstance(source, Mapping):
        errors.append(_error(
            "contract.source_missing",
            "Implementation contract requires a source mapping.",
            f"{location}.source",
        ))
        source = {}
    _reject_unknown_fields(
        source,
        {"task_id", "source_intent_sha256"},
        errors,
        f"{location}.source",
    )
    source_task_id = _required_string(source, "task_id", errors, f"{location}.source")
    source_hash = _required_string(source, "source_intent_sha256", errors, f"{location}.source")
    if source_hash and not SHA256_RE.fullmatch(source_hash):
        errors.append(_error(
            "contract.source_hash_invalid",
            "source.source_intent_sha256 must be a lowercase SHA-256 value.",
            f"{location}.source.source_intent_sha256",
        ))
    if expected_task_id is not None and source_task_id and source_task_id != expected_task_id:
        errors.append(_error(
            "contract.source_task_mismatch",
            f"Contract source task {source_task_id!r} does not match job task {expected_task_id!r}.",
            f"{location}.source.task_id",
        ))
    if (
        expected_source_intent_sha256 is not None
        and source_hash
        and source_hash != expected_source_intent_sha256
    ):
        errors.append(_error(
            "contract.source_hash_mismatch",
            "Contract source hash does not match the current user-authored task intent.",
            f"{location}.source.source_intent_sha256",
        ))

    profile = _required_string(raw, "profile", errors, location)
    if profile and profile not in IMPLEMENTATION_CONTRACT_PROFILES:
        errors.append(_error(
            "contract.profile_unknown",
            f"Unknown implementation contract profile: {profile}",
            f"{location}.profile",
        ))
    objective = _required_string(raw, "objective", errors, location)

    change_surface = _parse_change_surface(raw.get("change_surface"), errors, location)
    requirements = _required_string_tuple(raw.get("requirements"), "requirements", errors, location)
    interfaces = _parse_interfaces(raw.get("interfaces"), errors, location)
    failure_behavior = _optional_string_tuple(raw.get("failure_behavior"), "failure_behavior", errors, location)
    non_goals = _optional_string_tuple(raw.get("non_goals"), "non_goals", errors, location)
    focused_checks, invariants, acceptance_profiles = _parse_checks(raw.get("checks"), errors, location)
    context_excerpts = _parse_context_excerpts(raw.get("context_excerpts"), errors, location)

    for duplicate in _duplicates(requirements):
        errors.append(_error(
            "contract.requirement_duplicate",
            f"Requirement appears more than once: {duplicate}",
            f"{location}.requirements",
        ))

    if not focused_checks and not invariants and not acceptance_profiles:
        errors.append(_error(
            "contract.checks_missing",
            "Implementation contract requires at least one focused check, invariant, or acceptance profile.",
            f"{location}.checks",
        ))
    if profile == "integration" and not invariants:
        errors.append(_error(
            "contract.integration_invariant_missing",
            "Integration contracts require at least one project invariant.",
            f"{location}.checks.invariants",
        ))
    if profile == "documentation" and not any(
        pattern.endswith(".md") or ".md" in pattern
        for pattern in (*change_surface.add, *change_surface.edit)
    ):
        errors.append(_error(
            "contract.documentation_surface_missing",
            "Documentation contracts require a Markdown path in the allowed change surface.",
            f"{location}.change_surface",
        ))
    if profile == "bootstrap" and not change_surface.add:
        errors.append(_error(
            "contract.bootstrap_addition_missing",
            "Bootstrap contracts require at least one path in change_surface.add.",
            f"{location}.change_surface.add",
        ))
    if profile == "test_only":
        production_paths = tuple(
            path
            for path in (*change_surface.add, *change_surface.edit)
            if not _looks_like_test_path(path)
        )
        if production_paths:
            errors.append(_error(
                "contract.test_only_surface_invalid",
                (
                    "test_only contracts may change only test paths; found: "
                    f"{', '.join(production_paths)}."
                ),
                f"{location}.change_surface",
            ))

    if errors:
        return ContractParseResult(errors=tuple(errors))
    return ContractParseResult(contract=ImplementationContractDraft(
        schema=schema,
        source_task_id=source_task_id,
        source_intent_sha256=source_hash,
        profile=profile,
        objective=objective,
        change_surface=change_surface,
        requirements=requirements,
        focused_checks=focused_checks,
        invariants=invariants,
        acceptance_profiles=acceptance_profiles,
        interfaces=interfaces,
        failure_behavior=failure_behavior,
        non_goals=non_goals,
        context_excerpts=context_excerpts,
    ))


def _parse_context_excerpts(raw: object, errors: list[DomainError], location: str) -> tuple[ContextExcerpt, ...]:
    if raw is None:
        return ()
    items = _sequence(raw)
    if items is None:
        errors.append(_error("contract.context_excerpts_invalid", "context_excerpts must be a list.", f"{location}.context_excerpts"))
        return ()
    result: list[ContextExcerpt] = []
    for index, item in enumerate(items):
        place = f"{location}.context_excerpts[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_error("contract.context_excerpt_invalid", "Context excerpt must be a mapping.", place))
            continue
        _reject_unknown_fields(item, {"artifact", "heading"}, errors, place)
        result.append(ContextExcerpt(
            artifact=_required_string(item, "artifact", errors, place),
            heading=_required_string(item, "heading", errors, place),
        ))
    return tuple(result)


def _parse_change_surface(
    raw: object,
    errors: list[DomainError],
    location: str,
) -> ChangeSurface:
    field_location = f"{location}.change_surface"
    if not isinstance(raw, Mapping):
        errors.append(_error(
            "contract.change_surface_missing",
            "Implementation contract requires a change_surface mapping.",
            field_location,
        ))
        return ChangeSurface()
    _reject_unknown_fields(
        raw,
        {"add", "edit", "forbidden", "max_files", "max_changed_lines"},
        errors,
        field_location,
    )
    additions = _optional_string_tuple(raw.get("add"), "add", errors, field_location)
    edits = _optional_string_tuple(raw.get("edit"), "edit", errors, field_location)
    forbidden = _optional_string_tuple(raw.get("forbidden"), "forbidden", errors, field_location)
    max_files = _optional_positive_int(raw.get("max_files"), "max_files", errors, field_location)
    max_changed_lines = _optional_positive_int(raw.get("max_changed_lines"), "max_changed_lines", errors, field_location)
    if not additions and not edits:
        errors.append(_error(
            "contract.change_surface_empty",
            "change_surface must allow at least one addition or edit.",
            field_location,
        ))
    for field, values in (("add", additions), ("edit", edits), ("forbidden", forbidden)):
        for index, value in enumerate(values):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or value in {"", "."}:
                errors.append(_error(
                    "contract.path_invalid",
                    f"Contract paths must be non-empty workspace-relative paths: {value!r}.",
                    f"{field_location}.{field}[{index}]",
                ))
    allowed = set((*additions, *edits))
    for overlap in sorted(allowed.intersection(forbidden)):
        errors.append(_error(
            "contract.path_conflict",
            f"Path is both allowed and forbidden: {overlap}",
            field_location,
        ))
    for duplicate in _duplicates((*additions, *edits, *forbidden)):
        errors.append(_error(
            "contract.path_duplicate",
            f"Path appears more than once in the change surface: {duplicate}",
            field_location,
        ))
    return ChangeSurface(add=additions, edit=edits, forbidden=forbidden, max_files=max_files, max_changed_lines=max_changed_lines)


def _optional_positive_int(raw: object, field: str, errors: list[DomainError], location: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        errors.append(_error("contract.budget_invalid", f"{field} must be a positive integer.", f"{location}.{field}"))
        return None
    return raw


def _parse_checks(
    raw: object,
    errors: list[DomainError],
    location: str,
) -> tuple[tuple[ContractCheck, ...], tuple[str, ...], tuple[str, ...]]:
    field_location = f"{location}.checks"
    if not isinstance(raw, Mapping):
        errors.append(_error(
            "contract.checks_invalid",
            "Implementation contract requires a checks mapping.",
            field_location,
        ))
        return (), (), ()
    _reject_unknown_fields(
        raw,
        {"focused", "invariants", "profiles"},
        errors,
        field_location,
    )
    focused_raw = raw.get("focused", ())
    focused_items = _sequence(focused_raw)
    if focused_items is None:
        errors.append(_error(
            "contract.focused_checks_invalid",
            "checks.focused must be a list.",
            f"{field_location}.focused",
        ))
        focused_items = ()

    focused: list[ContractCheck] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(focused_items):
        check_location = f"{field_location}.focused[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_error(
                "contract.check_invalid",
                "Focused check must be a mapping.",
                check_location,
            ))
            continue
        _reject_unknown_fields(
            item,
            {"id", "argv", "timeout_seconds", "expect"},
            errors,
            check_location,
        )
        check_id = _required_string(item, "id", errors, check_location)
        if check_id and not CHECK_ID_RE.fullmatch(check_id):
            errors.append(_error(
                "contract.check_id_invalid",
                f"Focused check id is invalid: {check_id!r}.",
                f"{check_location}.id",
            ))
        if check_id in seen_ids:
            errors.append(_error(
                "contract.check_id_duplicate",
                f"Focused check id appears more than once: {check_id}",
                f"{check_location}.id",
            ))
        seen_ids.add(check_id)

        argv_raw = item.get("argv")
        argv_items = _sequence(argv_raw)
        argv: tuple[str, ...] = ()
        if argv_items is None or not argv_items:
            errors.append(_error(
                "contract.check_argv_missing",
                "Focused check requires a non-empty argv list.",
                f"{check_location}.argv",
            ))
        else:
            argv = tuple(str(part) for part in argv_items)
            if any(not part.strip() for part in argv):
                errors.append(_error(
                    "contract.check_argv_empty",
                    "Focused check argv entries must be non-empty.",
                    f"{check_location}.argv",
                ))
            controls = tuple(part for part in argv if part in SHELL_CONTROL_TOKENS)
            if controls:
                errors.append(_error(
                    "contract.check_shell_control",
                    f"Focused checks cannot contain shell control tokens: {', '.join(controls)}.",
                    f"{check_location}.argv",
                ))

        timeout = item.get("timeout_seconds", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            errors.append(_error(
                "contract.check_timeout_invalid",
                "Focused check timeout_seconds must be an integer from 1 to 3600.",
                f"{check_location}.timeout_seconds",
            ))
            timeout = 120
        expectation = _parse_expectation(item.get("expect"), errors, check_location)
        focused.append(ContractCheck(
            id=check_id,
            argv=argv,
            timeout_seconds=timeout,
            expect=expectation,
        ))

    invariants = _optional_string_tuple(raw.get("invariants"), "invariants", errors, field_location)
    for index, invariant in enumerate(invariants):
        if not CHECK_ID_RE.fullmatch(invariant):
            errors.append(_error(
                "contract.invariant_id_invalid",
                f"Project invariant id is invalid: {invariant!r}.",
                f"{field_location}.invariants[{index}]",
            ))
    for duplicate in _duplicates(invariants):
        errors.append(_error(
            "contract.invariant_duplicate",
            f"Project invariant appears more than once: {duplicate}",
            f"{field_location}.invariants",
        ))
    profiles = _optional_string_tuple(raw.get("profiles"), "profiles", errors, field_location)
    for index, profile in enumerate(profiles):
        if not CHECK_ID_RE.fullmatch(profile):
            errors.append(_error(
                "contract.acceptance_profile_id_invalid",
                f"Acceptance profile id is invalid: {profile!r}.",
                f"{field_location}.profiles[{index}]",
            ))
    for duplicate in _duplicates(profiles):
        errors.append(_error(
            "contract.acceptance_profile_duplicate",
            f"Acceptance profile appears more than once: {duplicate}",
            f"{field_location}.profiles",
        ))
    return tuple(focused), invariants, profiles


def _parse_expectation(
    raw: object,
    errors: list[DomainError],
    location: str,
) -> CheckExpectation:
    field_location = f"{location}.expect"
    if raw is None:
        return CheckExpectation()
    if not isinstance(raw, Mapping):
        errors.append(_error(
            "contract.check_expect_invalid",
            "Focused check expect must be a mapping.",
            field_location,
        ))
        return CheckExpectation()
    _reject_unknown_fields(
        raw,
        {"exit_code", "stdout_contains", "stderr_contains"},
        errors,
        field_location,
    )
    exit_code = raw.get("exit_code", 0)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        errors.append(_error(
            "contract.check_exit_code_invalid",
            "Focused check expect.exit_code must be an integer.",
            f"{field_location}.exit_code",
        ))
        exit_code = 0
    stdout_contains = _optional_string_tuple(
        raw.get("stdout_contains"),
        "stdout_contains",
        errors,
        field_location,
    )
    stderr_contains = _optional_string_tuple(
        raw.get("stderr_contains"),
        "stderr_contains",
        errors,
        field_location,
    )
    return CheckExpectation(
        exit_code=exit_code,
        stdout_contains=stdout_contains,
        stderr_contains=stderr_contains,
    )


def _required_string(
    raw: Mapping[object, object],
    key: str,
    errors: list[DomainError],
    location: str,
) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(_error(
            "contract.field_missing",
            f"Implementation contract field {key!r} must be a non-empty string.",
            f"{location}.{key}",
        ))
        return ""
    return value.strip()


def _required_string_tuple(
    raw: object,
    field: str,
    errors: list[DomainError],
    location: str,
) -> tuple[str, ...]:
    values = _optional_string_tuple(raw, field, errors, location)
    if not values:
        errors.append(_error(
            "contract.list_missing",
            f"Implementation contract field {field!r} must be a non-empty list.",
            f"{location}.{field}",
        ))
    return values


def _optional_string_tuple(
    raw: object,
    field: str,
    errors: list[DomainError],
    location: str,
) -> tuple[str, ...]:
    if raw is None:
        return ()
    items = _sequence(raw)
    if items is None:
        errors.append(_error(
            "contract.list_invalid",
            f"Implementation contract field {field!r} must be a list.",
            f"{location}.{field}",
        ))
        return ()
    values: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            errors.append(_error(
                "contract.list_item_invalid",
                f"Implementation contract field {field!r} contains an empty or non-string item.",
                f"{location}.{field}[{index}]",
            ))
            continue
        values.append(item.strip())
    return tuple(values)


def _parse_interfaces(
    raw: object,
    errors: list[DomainError],
    location: str,
) -> tuple[ContractInterface, ...]:
    if raw is None:
        return ()
    items = _sequence(raw)
    if items is None:
        errors.append(_error(
            "contract.list_invalid",
            "Implementation contract field 'interfaces' must be a list.",
            f"{location}.interfaces",
        ))
        return ()
    values: list[ContractInterface] = []
    for index, item in enumerate(items):
        item_location = f"{location}.interfaces[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_error(
                "contract.mapping_item_invalid",
                "Implementation contract field 'interfaces' contains a non-mapping item.",
                item_location,
            ))
            continue
        _reject_unknown_fields(
            item,
            {"name", "symbol", "signature", "behavior"},
            errors,
            item_location,
        )
        symbol_raw = item.get("symbol", item.get("name"))
        if not isinstance(symbol_raw, str) or not symbol_raw.strip():
            errors.append(_error(
                "contract.interface_symbol_missing",
                "Interface requires a non-empty symbol or name.",
                f"{item_location}.symbol",
            ))
            symbol = ""
        else:
            symbol = symbol_raw.strip()
        behavior = _required_string(item, "behavior", errors, item_location)
        signature_raw = item.get("signature")
        signature: str | None = None
        if signature_raw is not None:
            if not isinstance(signature_raw, str) or not signature_raw.strip():
                errors.append(_error(
                    "contract.interface_signature_invalid",
                    "Interface signature must be a non-empty string when present.",
                    f"{item_location}.signature",
                ))
            else:
                signature = signature_raw.strip()
        values.append(ContractInterface(
            symbol=symbol,
            behavior=behavior,
            signature=signature,
        ))
    return tuple(values)


def _sequence(raw: object) -> Sequence[object] | None:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return raw
    return None


def _duplicates(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)


def _reject_unknown_fields(
    raw: Mapping[object, object],
    allowed: set[str],
    errors: list[DomainError],
    location: str,
) -> None:
    for key in raw:
        if str(key) not in allowed:
            errors.append(_error(
                "contract.unknown_field",
                f"Unknown implementation contract field: {key}",
                f"{location}.{key}",
            ))


def _looks_like_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (
        any(part.lower() in {"test", "tests", "testing", "__tests__"} for part in parts)
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    )


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
