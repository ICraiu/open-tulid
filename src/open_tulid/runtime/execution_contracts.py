from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass, field, replace
from io import StringIO
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from ruamel.yaml import YAML

from open_tulid.domain import (
    DerivesDefinition,
    DomainError,
    ExecutionJob,
    OperationCallDefinition,
    RequirementDefinition,
    Task,
    TransactionDefinition,
    TransitionDefinition,
    ValidationCallDefinition,
)

from .repository_facts import (
    BaselineManifest,
    RepositoryFacts,
    baseline_manifest_to_dict,
    canonical_sha256,
    capture_repository_snapshot,
    repository_facts_to_dict,
)
from .prompt_versions import (
    PROMPT_COMPILER_VERSION,
    SUPPORTED_PROMPT_COMPILER_VERSIONS,
)
from .acceptance_profiles import AcceptanceProfile, load_acceptance_profiles
from .task_contracts import (
    CheckExpectation,
    ContractCheck,
    ImplementationContractDraft,
    PRODUCT_FACING_CONTRACT_PROFILES,
    SHELL_CONTROL_TOKENS,
    find_implementation_contract_path,
    parse_implementation_contract,
    task_source_intent_sha256,
    validate_task_implementation_contract,
)


EXECUTION_CONTRACT_SCHEMA = "tulid.execution/v1"
EXECUTION_CONTRACT_COMPILER_VERSION = 1
CONTEXT_EXCERPT_CHARACTER_LIMIT = 1_200
CONTEXT_EXCERPTS_TOTAL_CHARACTER_LIMIT = 2_000


@dataclass(frozen=True)
class ResolvedCheck:
    id: str
    source: str
    runner: str
    argv: tuple[str, ...] = ()
    validation_type: str | None = None
    validation_args: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    working_directory: str = "."
    timeout_seconds: int = 120
    expect: CheckExpectation = CheckExpectation()


@dataclass(frozen=True)
class FrozenContextExcerpt:
    artifact: str
    heading: str
    reason: str
    text: str
    sha256: str


@dataclass(frozen=True)
class ExecutionContract:
    source_task: Task
    transition: TransitionDefinition
    generated_contract: ImplementationContractDraft
    generated_contract_artifact_path: str
    generated_contract_sha256: str
    repository_facts: RepositoryFacts
    baseline_manifest: BaselineManifest
    resolved_checks: tuple[ResolvedCheck, ...]
    context_excerpts: tuple[FrozenContextExcerpt, ...]
    sha256: str


@dataclass(frozen=True)
class ExecutionContractResult:
    contract: ExecutionContract | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


def compile_task_execution_contract(
    *,
    project_root: Path,
    repo_root: Path | None,
    task: Task,
    transition: TransitionDefinition,
) -> ExecutionContractResult:
    parsed = validate_task_implementation_contract(project_root, task)
    if not parsed.accepted or parsed.contract is None:
        return ExecutionContractResult(errors=parsed.errors)
    contract_path = find_implementation_contract_path(project_root, task)
    if contract_path is None:
        return ExecutionContractResult(errors=(_error(
            "contract.artifact_missing",
            f"Task {task.id!r} has no readable linked ImplementationContract artifact.",
            task.id,
        ),))
    try:
        artifact_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    except OSError as exc:
        return ExecutionContractResult(errors=(_error(
            "contract.read_failed",
            f"Cannot read implementation contract: {exc}",
            str(contract_path),
        ),))
    try:
        artifact_path = contract_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return ExecutionContractResult(errors=(_error(
            "contract.path_escape",
            "Implementation contract artifact must stay inside the project tracker root.",
            str(contract_path),
        ),))

    repository = capture_repository_snapshot(repo_root)
    if not repository.accepted or repository.snapshot is None:
        return ExecutionContractResult(errors=repository.errors)
    checks, check_errors = _resolve_checks(project_root, parsed.contract, transition)
    if check_errors:
        return ExecutionContractResult(errors=check_errors)

    excerpts, excerpt_errors = _freeze_context_excerpts(project_root, task, parsed.contract.context_excerpts)
    if excerpt_errors:
        return ExecutionContractResult(errors=excerpt_errors)
    provisional = ExecutionContract(
        source_task=task,
        transition=transition,
        generated_contract=parsed.contract,
        generated_contract_artifact_path=artifact_path,
        generated_contract_sha256=artifact_sha256,
        repository_facts=repository.snapshot.facts,
        baseline_manifest=repository.snapshot.baseline,
        resolved_checks=checks,
        context_excerpts=excerpts,
        sha256="",
    )
    contract_hash = canonical_sha256(_execution_contract_body(provisional))
    return ExecutionContractResult(contract=replace(provisional, sha256=contract_hash))


def execution_contract_to_dict(contract: ExecutionContract) -> dict[str, object]:
    return {
        **_execution_contract_body(contract),
        "sha256": contract.sha256,
    }


def load_job_execution_contract(
    job: ExecutionJob,
    *,
    required: bool = False,
) -> ExecutionContractResult:
    raw = job.metadata.get("execution_contract")
    expected_hash = job.metadata.get("execution_contract_sha256")
    if raw is None and expected_hash is None:
        if required:
            return ExecutionContractResult(errors=(_error(
                "execution_contract.missing",
                f"Execution job {job.job_id!r} has no frozen execution contract.",
                job.job_id,
            ),))
        return ExecutionContractResult()
    if not isinstance(raw, Mapping) or not isinstance(expected_hash, str):
        return ExecutionContractResult(errors=(_error(
            "execution_contract.corrupt",
            "Frozen execution contract metadata is incomplete.",
            job.job_id,
        ),))

    payload = _json_value(raw)
    if not isinstance(payload, dict):
        return ExecutionContractResult(errors=(_error(
            "execution_contract.corrupt",
            "Frozen execution contract must be an object.",
            job.job_id,
        ),))
    embedded_hash = payload.pop("sha256", None)
    actual_hash = canonical_sha256(payload)
    prompt_compiler_version = payload.get("prompt_compiler_version")
    if (
        embedded_hash != expected_hash
        or actual_hash != expected_hash
        or payload.get("schema") != EXECUTION_CONTRACT_SCHEMA
        or payload.get("compiler_version") != EXECUTION_CONTRACT_COMPILER_VERSION
        or isinstance(prompt_compiler_version, bool)
        or prompt_compiler_version not in SUPPORTED_PROMPT_COMPILER_VERSIONS
    ):
        return ExecutionContractResult(errors=(_error(
            "execution_contract.hash_mismatch",
            "Frozen execution contract failed its integrity check.",
            job.job_id,
        ),))

    try:
        assert isinstance(prompt_compiler_version, int)
        source_task = _task_from_dict(_mapping(payload.get("source"), "source").get("task"))
        transition = _transition_from_dict(payload.get("transition"))
        generated = _implementation_contract_from_dict(payload.get("generated_contract"))
        repository = _mapping(payload.get("repository"), "repository")
        facts = _repository_facts_from_dict(repository.get("facts"))
        baseline = _baseline_manifest_from_dict(repository.get("baseline_manifest"))
        resolved_checks = _resolved_checks_from_list(payload.get("resolved_checks"))
        context_excerpts = _context_excerpts_from_list(
            payload.get("context_excerpts", ()),
            legacy_missing_reason=prompt_compiler_version == 1,
        )
        artifact_path = _required_string(
            _mapping(payload.get("generated_contract"), "generated_contract"),
            "artifact_path",
        )
        artifact_hash = _required_string(
            _mapping(payload.get("generated_contract"), "generated_contract"),
            "artifact_sha256",
        )
    except (TypeError, ValueError, KeyError) as exc:
        return ExecutionContractResult(errors=(_error(
            "execution_contract.corrupt",
            f"Frozen execution contract cannot be loaded: {exc}",
            job.job_id,
        ),))

    if (
        source_task.id != job.task_id
        or transition.id != job.transition_id
        or transition.worker != job.worker_id
    ):
        return ExecutionContractResult(errors=(_error(
            "execution_contract.job_mismatch",
            "Frozen execution contract does not match its execution job.",
            job.job_id,
        ),))
    return ExecutionContractResult(contract=ExecutionContract(
        source_task=source_task,
        transition=transition,
        generated_contract=generated,
        generated_contract_artifact_path=artifact_path,
        generated_contract_sha256=artifact_hash,
        repository_facts=facts,
        baseline_manifest=baseline,
        resolved_checks=resolved_checks,
        context_excerpts=context_excerpts,
        sha256=expected_hash,
    ))


def _execution_contract_body(contract: ExecutionContract) -> dict[str, object]:
    transition = _transition_to_dict(contract.transition)
    return {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "compiler_version": EXECUTION_CONTRACT_COMPILER_VERSION,
        "prompt_compiler_version": PROMPT_COMPILER_VERSION,
        "source": {
            "task": _task_to_dict(contract.source_task),
            "source_intent_sha256": task_source_intent_sha256(contract.source_task),
        },
        "generated_contract": {
            **_implementation_contract_to_dict(contract.generated_contract),
            "artifact_path": contract.generated_contract_artifact_path,
            "artifact_sha256": contract.generated_contract_sha256,
        },
        "transition": transition,
        "transition_sha256": canonical_sha256(transition),
        "repository": {
            "facts": repository_facts_to_dict(contract.repository_facts),
            "baseline_manifest": baseline_manifest_to_dict(contract.baseline_manifest),
        },
        "resolved_checks": [
            _resolved_check_to_dict(check)
            for check in contract.resolved_checks
        ],
        "context_excerpts": [
            {
                "artifact": excerpt.artifact,
                "heading": excerpt.heading,
                "reason": excerpt.reason,
                "text": excerpt.text,
                "sha256": excerpt.sha256,
            }
            for excerpt in contract.context_excerpts
        ],
    }


def _resolve_checks(
    project_root: Path,
    contract: ImplementationContractDraft,
    transition: TransitionDefinition,
) -> tuple[tuple[ResolvedCheck, ...], tuple[DomainError, ...]]:
    checks: dict[str, ResolvedCheck] = {}
    errors: list[DomainError] = []
    for check in contract.focused_checks:
        checks[check.id] = _focused_check(check)

    profiles = load_acceptance_profiles(project_root)
    if not profiles.accepted or profiles.profiles is None:
        return (), profiles.errors
    for profile_id in contract.acceptance_profiles:
        profile = profiles.profiles.get(profile_id)
        if profile is None:
            errors.append(_error(
                "execution_contract.acceptance_profile_unknown",
                f"Contract selects unknown acceptance profile: {profile_id}",
                profile_id,
            ))
            continue
        if profile_id in checks:
            errors.append(_error(
                "execution_contract.check_conflict",
                f"Acceptance profile id conflicts with a focused check: {profile_id}",
                profile_id,
            ))
            continue
        checks[profile_id] = ResolvedCheck(
            id=profile.id,
            source="acceptance_profile",
            runner="command",
            argv=profile.argv,
            working_directory=profile.working_directory,
            timeout_seconds=profile.timeout_seconds,
            expect=profile.expect,
        )

    _validate_vertical_slice_policy(
        contract,
        profiles.profiles,
        profiles.require_vertical_slice,
        errors,
    )

    transition_calls: dict[str, ValidationCallDefinition] = {}
    for call in transition.requires.validations:
        if call.type in transition_calls:
            errors.append(_error(
                "execution_contract.validation_duplicate",
                f"Transition validation id appears more than once: {call.type}",
                transition.id,
            ))
            continue
        transition_calls[call.type] = call
        resolved, command_error = _transition_check(call)
        if command_error is not None:
            errors.append(command_error)
            continue
        existing = checks.get(resolved.id)
        if existing is None:
            checks[resolved.id] = resolved
            continue
        if not _same_check(existing, resolved):
            errors.append(_error(
                "execution_contract.check_conflict",
                (
                    f"Task check {resolved.id!r} conflicts with the transition "
                    "validation of the same id."
                ),
                resolved.id,
            ))
            continue
        checks[resolved.id] = replace(
            existing,
            source="task+transition",
            validation_type=resolved.validation_type,
            validation_args=resolved.validation_args,
        )

    for invariant in contract.invariants:
        if invariant not in transition_calls:
            errors.append(_error(
                "execution_contract.invariant_unknown",
                f"Contract selects unknown project invariant: {invariant}",
                invariant,
            ))
    return (
        tuple(checks[key] for key in sorted(checks)),
        tuple(errors),
    )


def _validate_vertical_slice_policy(
    contract: ImplementationContractDraft,
    profiles: Mapping[str, AcceptanceProfile],
    require_vertical_slice: bool,
    errors: list[DomainError],
) -> None:
    """Enforce vertical-slice evidence for product-facing implementation work."""
    selected = [
        profiles[profile_id]
        for profile_id in contract.acceptance_profiles
        if profile_id in profiles
    ]
    has_vertical_slice = any(profile.kind == "vertical_slice" for profile in selected)
    if contract.profile not in PRODUCT_FACING_CONTRACT_PROFILES:
        if contract.vertical_slice_exemption is not None:
            errors.append(_error(
                "execution_contract.vertical_slice_exemption_unneeded",
                "Only product-facing contracts may declare a vertical-slice exemption.",
                "checks.vertical_slice_exemption",
            ))
        return
    if has_vertical_slice and contract.vertical_slice_exemption is not None:
        errors.append(_error(
            "execution_contract.vertical_slice_exemption_conflict",
            "Select a vertical-slice profile or record an exemption, not both.",
            "checks.vertical_slice_exemption",
        ))
    elif (
        require_vertical_slice
        and not has_vertical_slice
        and contract.vertical_slice_exemption is None
    ):
        errors.append(_error(
            "execution_contract.vertical_slice_required",
            "Product-facing contracts require a selected vertical-slice acceptance profile or an explicit exemption.",
            "checks.profiles",
        ))


def _focused_check(check: ContractCheck) -> ResolvedCheck:
    return ResolvedCheck(
        id=check.id,
        source="task",
        runner="command",
        argv=check.argv,
        timeout_seconds=check.timeout_seconds,
        expect=check.expect,
    )


def _freeze_context_excerpts(project_root: Path, task: Task, selections) -> tuple[tuple[FrozenContextExcerpt, ...], tuple[DomainError, ...]]:
    frozen: list[FrozenContextExcerpt] = []
    errors: list[DomainError] = []
    total_characters = 0
    for selection in selections:
        root = project_root.resolve()
        candidates = []
        for link in task.artifact_links:
            relative = Path(link)
            if selection.artifact not in relative.parts or relative.is_absolute() or ".." in relative.parts:
                continue
            candidate = (root / relative).resolve()
            if candidate != root and root not in candidate.parents:
                continue
            candidates.append(candidate)
        if len(candidates) != 1 or not candidates[0].is_file():
            errors.append(_error("execution_contract.context_artifact_missing", f"Context artifact {selection.artifact!r} must resolve to one linked file.", selection.artifact))
            continue
        try:
            source = candidates[0].read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(_error("execution_contract.context_read_failed", f"Cannot read context artifact: {exc}", str(candidates[0])))
            continue
        excerpts = _markdown_heading_excerpts(source, selection.heading)
        if not excerpts:
            errors.append(_error("execution_contract.context_heading_missing", f"Heading {selection.heading!r} was not found in {selection.artifact!r}.", selection.heading))
            continue
        if len(excerpts) > 1:
            errors.append(_error(
                "execution_contract.context_heading_duplicate",
                f"Heading {selection.heading!r} appears more than once in {selection.artifact!r}.",
                selection.heading,
            ))
            continue
        excerpt = excerpts[0]
        if len(excerpt) > CONTEXT_EXCERPT_CHARACTER_LIMIT:
            errors.append(_error(
                "execution_contract.context_excerpt_too_large",
                (
                    f"Context excerpt {selection.heading!r} exceeds the "
                    f"{CONTEXT_EXCERPT_CHARACTER_LIMIT}-character per-excerpt budget."
                ),
                selection.heading,
            ))
            continue
        total_characters += len(excerpt)
        if total_characters > CONTEXT_EXCERPTS_TOTAL_CHARACTER_LIMIT:
            errors.append(_error(
                "execution_contract.context_excerpts_too_large",
                (
                    "Selected context excerpts exceed the "
                    f"{CONTEXT_EXCERPTS_TOTAL_CHARACTER_LIMIT}-character total budget."
                ),
                "context_excerpts",
            ))
            continue
        frozen.append(FrozenContextExcerpt(
            selection.artifact,
            selection.heading,
            selection.reason,
            excerpt,
            hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        ))
    return tuple(frozen), tuple(errors)


def _markdown_heading_excerpts(source: str, heading: str) -> tuple[str, ...]:
    lines = source.splitlines()
    target = heading.strip().lstrip("#").strip().casefold()
    starts: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        hashes, _, title = stripped.partition(" ")
        if title.strip().casefold() == target:
            starts.append((index, len(hashes)))
    excerpts: list[str] = []
    for start, level in starts:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            stripped = lines[index].lstrip()
            if stripped.startswith("#"):
                hashes, _, _title = stripped.partition(" ")
                if len(hashes) <= level:
                    end = index
                    break
        excerpts.append("\n".join(lines[start:end]).strip())
    return tuple(excerpts)


def _transition_check(
    call: ValidationCallDefinition,
) -> tuple[ResolvedCheck, DomainError | None]:
    args = _json_value(call.args)
    if not isinstance(args, dict):
        args = {}
    command = args.get("command")
    if command is None:
        return ResolvedCheck(
            id=call.type,
            source="transition",
            runner="validation",
            validation_type=call.type,
            validation_args=MappingProxyType(args),
        ), None
    try:
        argv = _command_argv(command)
    except ValueError as exc:
        return ResolvedCheck(id=call.type, source="transition", runner="command"), _error(
            "execution_contract.command_invalid",
            f"Transition validation {call.type!r} has an invalid command: {exc}",
            call.type,
        )
    controls = tuple(part for part in argv if part in SHELL_CONTROL_TOKENS)
    if controls:
        return ResolvedCheck(id=call.type, source="transition", runner="command"), _error(
            "execution_contract.command_shell_control",
            (
                f"Transition validation {call.type!r} contains shell control tokens: "
                f"{', '.join(controls)}."
            ),
            call.type,
        )
    return ResolvedCheck(
        id=call.type,
        source="transition",
        runner="command",
        argv=argv,
        validation_type=call.type,
        validation_args=MappingProxyType(args),
    ), None


def _command_argv(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            argv = tuple(shlex.split(raw))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        argv = tuple(str(part) for part in raw)
    else:
        raise ValueError("command must be a string or argument array")
    if not argv or any(not part for part in argv):
        raise ValueError("command must contain at least one non-empty argument")
    return argv


def _same_check(left: ResolvedCheck, right: ResolvedCheck) -> bool:
    return (
        left.runner == right.runner
        and left.argv == right.argv
        and left.working_directory == right.working_directory
        and left.expect == right.expect
    )


def _resolved_check_to_dict(check: ResolvedCheck) -> dict[str, object]:
    return {
        "id": check.id,
        "source": check.source,
        "runner": check.runner,
        "argv": list(check.argv),
        "validation_type": check.validation_type,
        "validation_args": _json_value(check.validation_args),
        "working_directory": check.working_directory,
        "timeout_seconds": check.timeout_seconds,
        "expect": {
            "exit_code": check.expect.exit_code,
            "stdout_contains": list(check.expect.stdout_contains),
            "stderr_contains": list(check.expect.stderr_contains),
        },
    }


def _implementation_contract_to_dict(
    contract: ImplementationContractDraft,
) -> dict[str, object]:
    return {
        "schema": contract.schema,
        "source": {
            "task_id": contract.source_task_id,
            "source_intent_sha256": contract.source_intent_sha256,
        },
        "profile": contract.profile,
        "objective": contract.objective,
        "change_surface": {
            "add": list(contract.change_surface.add),
            "edit": list(contract.change_surface.edit),
            "forbidden": list(contract.change_surface.forbidden),
            "max_files": contract.change_surface.max_files,
            "max_changed_lines": contract.change_surface.max_changed_lines,
        },
        "interfaces": [
            {
                "symbol": interface.symbol,
                "signature": interface.signature,
                "behavior": interface.behavior,
            }
            for interface in contract.interfaces
        ],
        "requirements": list(contract.requirements),
        "failure_behavior": list(contract.failure_behavior),
        "non_goals": list(contract.non_goals),
        "context_excerpts": [
            {
                "artifact": excerpt.artifact,
                "heading": excerpt.heading,
                "reason": excerpt.reason,
            }
            for excerpt in contract.context_excerpts
        ],
        "checks": {
            "focused": [
                {
                    "id": check.id,
                    "argv": list(check.argv),
                    "timeout_seconds": check.timeout_seconds,
                    "expect": {
                        "exit_code": check.expect.exit_code,
                        "stdout_contains": list(check.expect.stdout_contains),
                        "stderr_contains": list(check.expect.stderr_contains),
                    },
                }
                for check in contract.focused_checks
            ],
            "invariants": list(contract.invariants),
            "profiles": list(contract.acceptance_profiles),
            "vertical_slice_exemption": contract.vertical_slice_exemption,
        },
    }


def _implementation_contract_from_dict(raw: object) -> ImplementationContractDraft:
    # Reuse the public parser so frozen contracts follow the same v1 schema.
    payload = _mapping(raw, "generated_contract")
    contract_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"artifact_path", "artifact_sha256"}
    }
    stream = StringIO()
    YAML().dump(contract_payload, stream)
    parsed = parse_implementation_contract(stream.getvalue())
    if not parsed.accepted or parsed.contract is None:
        codes = ", ".join(error.code for error in parsed.errors)
        raise ValueError(f"generated contract is invalid: {codes}")
    return parsed.contract


def _task_to_dict(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "title": task.title,
        "path": task.path,
        "current_state": task.current_state,
        "task_type": task.task_type,
        "dependencies": list(task.dependencies),
        "artifact_links": list(task.artifact_links),
        "parent_id": task.parent_id,
        "metadata": _json_value(task.metadata),
        "body": task.body,
    }


def _task_from_dict(raw: object) -> Task:
    payload = _mapping(raw, "source.task")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("source.task.metadata must be an object")
    parent_id = payload.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, str):
        raise ValueError("source.task.parent_id must be a string or null")
    return Task(
        id=_required_string(payload, "id"),
        title=_required_string(payload, "title"),
        path=_required_string(payload, "path"),
        current_state=_required_string(payload, "current_state"),
        task_type=_required_string(payload, "task_type"),
        dependencies=_string_tuple(payload.get("dependencies")),
        artifact_links=_string_tuple(payload.get("artifact_links")),
        parent_id=parent_id,
        metadata=MappingProxyType(dict(metadata)),
        body=str(payload.get("body", "")),
    )


def _transition_to_dict(transition: TransitionDefinition) -> dict[str, object]:
    return {
        "id": transition.id,
        "task_type": transition.task_type,
        "from_state": transition.from_state,
        "to_state": transition.to_state,
        "worker": transition.worker,
        "requires": {
            "artifacts": list(transition.requires.artifacts),
            "validations": [
                {
                    "type": call.type,
                    "args": _json_value(call.args),
                }
                for call in transition.requires.validations
            ],
            "changed_files_required": transition.requires.changed_files_required,
        },
        "transaction": (
            {
                "steps": [
                    {
                        "op": step.op,
                        "args": _json_value(step.args),
                    }
                    for step in transition.transaction.steps
                ],
            }
            if transition.transaction is not None
            else None
        ),
        "derives": (
            {
                "task_type": transition.derives.task_type,
                "state": transition.derives.state,
                "artifact_type": transition.derives.artifact_type,
                "required": transition.derives.required,
                "parent_to_if_derived": transition.derives.parent_to_if_derived,
            }
            if transition.derives is not None
            else None
        ),
        "default_for_scheduler": transition.default_for_scheduler,
        "instructions": list(transition.instructions),
    }


def _transition_from_dict(raw: object) -> TransitionDefinition:
    payload = _mapping(raw, "transition")
    requires_payload = _mapping(payload.get("requires"), "transition.requires")
    validation_items = requires_payload.get("validations", ())
    if not isinstance(validation_items, Sequence) or isinstance(validation_items, (str, bytes)):
        raise ValueError("transition.requires.validations must be a list")
    validations: list[ValidationCallDefinition] = []
    for item in validation_items:
        call = _mapping(item, "transition.requires.validations[]")
        args = call.get("args", {})
        if not isinstance(args, Mapping):
            raise ValueError("transition validation args must be an object")
        validations.append(ValidationCallDefinition(
            type=_required_string(call, "type"),
            args=MappingProxyType(dict(args)),
        ))

    transaction_payload = payload.get("transaction")
    transaction = None
    if transaction_payload is not None:
        tx = _mapping(transaction_payload, "transition.transaction")
        raw_steps = tx.get("steps", ())
        if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
            raise ValueError("transition.transaction.steps must be a list")
        steps: list[OperationCallDefinition] = []
        for item in raw_steps:
            step = _mapping(item, "transition.transaction.steps[]")
            args = step.get("args", {})
            if not isinstance(args, Mapping):
                raise ValueError("transition operation args must be an object")
            steps.append(OperationCallDefinition(
                op=_required_string(step, "op"),
                args=MappingProxyType(dict(args)),
            ))
        transaction = TransactionDefinition(steps=tuple(steps))

    derives_payload = payload.get("derives")
    derives = None
    if derives_payload is not None:
        derives_map = _mapping(derives_payload, "transition.derives")
        derives_required = derives_map.get("required", True)
        if not isinstance(derives_required, bool):
            raise ValueError("transition.derives.required must be a boolean")
        parent_to_if_derived = derives_map.get("parent_to_if_derived")
        if parent_to_if_derived is not None and (
            not isinstance(parent_to_if_derived, str) or not parent_to_if_derived
        ):
            raise ValueError(
                "transition.derives.parent_to_if_derived must be a non-empty string or null"
            )
        derives = DerivesDefinition(
            task_type=_required_string(derives_map, "task_type"),
            state=_required_string(derives_map, "state"),
            artifact_type=_required_string(derives_map, "artifact_type"),
            required=derives_required,
            parent_to_if_derived=parent_to_if_derived,
        )

    worker = payload.get("worker")
    if worker is not None and not isinstance(worker, str):
        raise ValueError("transition.worker must be a string or null")
    return TransitionDefinition(
        id=_required_string(payload, "id"),
        task_type=_required_string(payload, "task_type"),
        from_state=_required_string(payload, "from_state"),
        to_state=_required_string(payload, "to_state"),
        worker=worker,
        requires=RequirementDefinition(
            artifacts=_string_tuple(requires_payload.get("artifacts")),
            validations=tuple(validations),
            changed_files_required=bool(requires_payload.get("changed_files_required", False)),
        ),
        transaction=transaction,
        derives=derives,
        default_for_scheduler=bool(payload.get("default_for_scheduler", False)),
        instructions=_string_tuple(payload.get("instructions")),
    )


def _repository_facts_from_dict(raw: object) -> RepositoryFacts:
    payload = _mapping(raw, "repository.facts")
    base_commit = payload.get("base_commit")
    if base_commit is not None and not isinstance(base_commit, str):
        raise ValueError("repository base_commit must be a string or null")
    dirty = payload.get("dirty")
    if dirty is not None and not isinstance(dirty, bool):
        raise ValueError("repository dirty must be a boolean or null")
    return RepositoryFacts(
        schema=_required_string(payload, "schema"),
        repository_available=bool(payload.get("repository_available", False)),
        git_repository=bool(payload.get("git_repository", False)),
        base_commit=base_commit,
        dirty=dirty,
        top_level_entries=_string_tuple(payload.get("top_level_entries")),
        manifests=_string_tuple(payload.get("manifests")),
        detected_entrypoints=_string_tuple(payload.get("detected_entrypoints")),
        file_count=int(payload.get("file_count", 0)),
        total_bytes=int(payload.get("total_bytes", 0)),
        sha256=_required_string(payload, "sha256"),
    )


def _baseline_manifest_from_dict(raw: object) -> BaselineManifest:
    from .repository_facts import FileManifestEntry

    payload = _mapping(raw, "repository.baseline_manifest")
    raw_entries = payload.get("entries", ())
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        raise ValueError("baseline manifest entries must be a list")
    entries = tuple(
        FileManifestEntry(
            path=_required_string(_mapping(item, "baseline entry"), "path"),
            sha256=_required_string(_mapping(item, "baseline entry"), "sha256"),
            size=int(_mapping(item, "baseline entry").get("size", 0)),
        )
        for item in raw_entries
    )
    return BaselineManifest(
        schema=_required_string(payload, "schema"),
        entries=entries,
        sha256=_required_string(payload, "sha256"),
    )


def _resolved_checks_from_list(raw: object) -> tuple[ResolvedCheck, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("resolved_checks must be a list")
    checks: list[ResolvedCheck] = []
    for item in raw:
        payload = _mapping(item, "resolved_checks[]")
        expect = _mapping(payload.get("expect"), "resolved_checks[].expect")
        validation_args = payload.get("validation_args", {})
        if not isinstance(validation_args, Mapping):
            raise ValueError("resolved check validation_args must be an object")
        validation_type = payload.get("validation_type")
        if validation_type is not None and not isinstance(validation_type, str):
            raise ValueError("resolved check validation_type must be a string or null")
        checks.append(ResolvedCheck(
            id=_required_string(payload, "id"),
            source=_required_string(payload, "source"),
            runner=_required_string(payload, "runner"),
            argv=_string_tuple(payload.get("argv")),
            validation_type=validation_type,
            validation_args=MappingProxyType(dict(validation_args)),
            working_directory=_required_string(payload, "working_directory"),
            timeout_seconds=int(payload.get("timeout_seconds", 120)),
            expect=CheckExpectation(
                exit_code=int(expect.get("exit_code", 0)),
                stdout_contains=_string_tuple(expect.get("stdout_contains")),
                stderr_contains=_string_tuple(expect.get("stderr_contains")),
            ),
        ))
    return tuple(checks)


def _context_excerpts_from_list(
    raw: object,
    *,
    legacy_missing_reason: bool = False,
) -> tuple[FrozenContextExcerpt, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("context_excerpts must be a list")
    excerpts: list[FrozenContextExcerpt] = []
    for item in raw:
        payload = _mapping(item, "context_excerpts[]")
        text = _required_string(payload, "text")
        sha256 = _required_string(payload, "sha256")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != sha256:
            raise ValueError("context excerpt hash mismatch")
        reason = payload.get("reason")
        if reason is None and legacy_missing_reason:
            reason = "Required to implement the generated contract."
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        excerpts.append(FrozenContextExcerpt(
            artifact=_required_string(payload, "artifact"),
            heading=_required_string(payload, "heading"),
            reason=reason,
            text=text,
            sha256=sha256,
        ))
    return tuple(excerpts)


def _mapping(raw: object, name: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be an object")
    return raw


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _string_tuple(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("value must be a list of strings")
    values = tuple(str(item) for item in raw)
    if any(not value for value in values):
        raise ValueError("list values must be non-empty strings")
    return values


def _json_value(raw: object) -> object:
    if isinstance(raw, Mapping):
        return {
            str(key): _json_value(value)
            for key, value in raw.items()
        }
    if isinstance(raw, (list, tuple)):
        return [_json_value(item) for item in raw]
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return raw
    return str(raw)


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
