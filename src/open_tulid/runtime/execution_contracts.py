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
from .standard_contracts import (
    CommandExpectation,
    ProjectCommand,
    StandardContract,
    load_standard_contract,
)
from .context import (
    LinkedContextResolver,
    render_context_document,
    task_for_context,
)
from .task_contracts import (
    ChangeSurface,
    CheckExpectation,
    ContractCheck,
    ImplementationContractDraft,
    SHELL_CONTROL_TOKENS,
    find_implementation_contract_path,
    parse_implementation_contract,
    task_source_intent_sha256,
    validate_task_implementation_contract,
)


EXECUTION_CONTRACT_SCHEMA = "tulid.execution/v1"
EXECUTION_CONTRACT_COMPILER_VERSION = 1
GLOBAL_IMPLEMENTATION_CONTRACT_SCHEMA = "tulid.global_contract/v1"


@dataclass(frozen=True)
class FrozenContextExcerpt:
    artifact: str
    heading: str
    reason: str
    text: str
    sha256: str
    # Relative workspace path (under .open-tulid/context/) holding the complete
    # frozen source bytes this excerpt is clipped from, if any.
    context_file_path: str | None = None


@dataclass(frozen=True)
class FrozenContextFile:
    """Complete frozen source bytes with provenance.

    The full document is stored with the job's durable inputs and materialized
    under the internal workspace area (``.open-tulid/context/``) so a worker can
    always read the whole required source. ``refs`` retains every original
    reference that resolved to these byte-identical contents after
    deduplication.
    """
    workspace_path: str
    content: str
    sha256: str
    byte_count: int
    required: bool
    reason: str
    role: str = "reference"
    refs: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()


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
class ExecutionContract:
    source_task: Task
    transition: TransitionDefinition
    generated_contract: ImplementationContractDraft
    generated_contract_artifact_path: str
    generated_contract_sha256: str
    repository_facts: RepositoryFacts
    baseline_manifest: BaselineManifest
    resolved_checks: tuple[ResolvedCheck, ...]
    # file/directory diffs are recorded as evidence but are NOT acceptance
    # criteria; a worker may freely create/edit/rename/delete task files.
    context_excerpts: tuple = ()
    # Complete frozen source bytes with provenance, materialized under
    # .open-tulid/context/ in the prepared workspace. Excluded from promotion.
    context_files: tuple = ()
    sha256: str = ""

    @property
    def commands(self) -> tuple[ProjectCommand, ...]:
        """Snapshot of the global contract commands that this execution verifies."""
        commands: list[ProjectCommand] = []
        for check in self.resolved_checks:
            if check.runner != "command":
                continue
            commands.append(ProjectCommand(
                name=check.id,
                argv=check.argv,
                working_directory=check.working_directory,
                timeout_seconds=check.timeout_seconds,
                expect=CommandExpectation(exit_code=check.expect.exit_code),
            ))
        return tuple(commands)


@dataclass(frozen=True)
class ExecutionContractResult:
    contract: ExecutionContract | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


def source_content_identities(contract: ExecutionContract) -> tuple[tuple[str, str], ...]:
    """Original reference plus content digest for each required frozen source.

    The returned ``(ref, sha256)`` identities are the ``selected required source
    content`` a task depends on: its specification, canonical answers, and any
    binding generated contract that were frozen at job creation. Unrequired
    background references discovered by wiki links are excluded so a worker's
    optional reading never alters the semantic revision. Identities are deduped
    and canonically ordered for a stable task revision.
    """
    identities: set[tuple[str, str]] = set()
    for context_file in contract.context_files:
        if not context_file.required:
            continue
        refs = context_file.refs or (context_file.source_paths or ("[reference]",))
        for ref in refs:
            identities.add((ref, context_file.sha256))
    return tuple(sorted(identities, key=lambda item: (item[0], item[1])))


def compile_standard_execution_contract(
    *,
    project_root: Path,
    repo_root: Path | None,
    task: Task,
    transition: TransitionDefinition,
    parent_tasks: tuple[Task, ...] = (),
) -> ExecutionContractResult:
    """Compile an execution contract from the project's single global command
    contract (``contract.yaml``).

    This is the live implementation/self-review path. The global contract is the
    only source of verification commands; there is no per-task LLM-authored
    ImplementationContract and no file allowlist.

    The task's linked context — its specification/answer lineage and relevant
    linked references — is resolved here at job creation and frozen into the
    contract, so the assigned worker never rebuilds planning decisions from a
    mutable vault after scheduling.
    """
    standard = load_standard_contract(project_root)
    if not standard.accepted or standard.contract is None:
        if standard.errors:
            return ExecutionContractResult(errors=standard.errors)
        return ExecutionContractResult(errors=(_error(
            "contract.missing",
            "Implementation transitions require a project global contract.yaml.",
            str(project_root / "contract.yaml"),
        ),))

    standard_contract: StandardContract = standard.contract
    command_errors = _validate_global_commands(standard_contract)
    if command_errors:
        return ExecutionContractResult(errors=tuple(command_errors))

    repository = capture_repository_snapshot(repo_root)
    if not repository.accepted or repository.snapshot is None:
        return ExecutionContractResult(errors=repository.errors)

    checks = tuple(
        _global_command_check(cmd) for cmd in standard_contract.commands
    )
    context_excerpts, context_files, context_errors = _freeze_linked_context(
        project_root,
        task,
        transition,
        parent_tasks=parent_tasks,
    )
    if context_errors:
        return ExecutionContractResult(errors=context_errors)
    draft = _global_contract_draft(task, standard_contract)
    provisional = ExecutionContract(
        source_task=task,
        transition=transition,
        generated_contract=draft,
        generated_contract_artifact_path="",
        generated_contract_sha256="",
        repository_facts=repository.snapshot.facts,
        baseline_manifest=repository.snapshot.baseline,
        resolved_checks=checks,
        context_excerpts=context_excerpts,
        context_files=context_files,
        sha256="",
    )
    contract_hash = canonical_sha256(_execution_contract_body(provisional))
    return ExecutionContractResult(contract=replace(provisional, sha256=contract_hash))


def _global_contract_draft(
    task: Task,
    standard: StandardContract,
) -> ImplementationContractDraft:
    """Synthesize the task-facing generated contract from the global command
    policy. There is intentionally no change surface, no interfaces, no
    invariants, no acceptance profiles, and no per-task authorship."""
    return ImplementationContractDraft(
        schema=GLOBAL_IMPLEMENTATION_CONTRACT_SCHEMA,
        source_task_id=task.id,
        source_intent_sha256=task_source_intent_sha256(task),
        profile="code_change",
        objective=task.body.strip() or f"Implement {task.title.strip()}.",
        change_surface=ChangeSurface(add=(), edit=(), forbidden=()),
        requirements=(),
        focused_checks=(),
        invariants=(),
        acceptance_profiles=(),
        context_excerpts=(),
    )


def _validate_global_commands(standard: StandardContract) -> tuple[DomainError, ...]:
    """Structural guard: a global contract must resolve at least one command and
    every command must be a deterministic, non-empty argv array. Malformed
    definitions surface here as clear project configuration errors."""
    errors: list[DomainError] = []
    if not standard.commands:
        errors.append(_error(
            "contract.commands_missing",
            "Project global contract must define at least one verification command.",
            "contract.yaml:commands",
        ))
    for command in standard.commands:
        if not command.argv:
            errors.append(_error(
                "contract.command_argv_empty",
                f"Contract command {command.name!r} has an empty argv array.",
                f"contract.yaml:commands.{command.name}",
            ))
    return tuple(errors)


def _global_command_check(command: ProjectCommand) -> ResolvedCheck:
    return ResolvedCheck(
        id=command.name,
        source="standard",
        runner="command",
        argv=command.argv,
        working_directory=command.working_directory,
        timeout_seconds=command.timeout_seconds,
        expect=CheckExpectation(exit_code=command.expect.exit_code),
    )


def compile_task_execution_contract(
    *,
    project_root: Path,
    repo_root: Path | None,
    task: Task,
    transition: TransitionDefinition,
) -> ExecutionContractResult:
    """LEGACY: compile an execution contract from a per-task LLM-authored
    ImplementationContract artifact.

    Retained only so historical per-task artifacts remain readable/migratable.
    It is not used by new runs: implementation tasks schedule directly under the
    project global command contract and never require such an artifact.
    """
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
        source_task = _task_from_dict(_mapping(payload.get("source"), "source").get("task"))
        transition = _transition_from_dict(payload.get("transition"))
        generated_payload = _mapping(payload.get("generated_contract"), "generated_contract")
        if generated_payload.get("schema") == GLOBAL_IMPLEMENTATION_CONTRACT_SCHEMA:
            generated = _global_draft_from_dict(generated_payload)
            artifact_path = str(generated_payload.get("artifact_path", ""))
            artifact_hash = str(generated_payload.get("artifact_sha256", ""))
        else:
            generated = _implementation_contract_from_dict(generated_payload)
            artifact_path = _required_string(generated_payload, "artifact_path")
            artifact_hash = _required_string(generated_payload, "artifact_sha256")
        repository = _mapping(payload.get("repository"), "repository")
        facts = _repository_facts_from_dict(repository.get("facts"))
        baseline = _baseline_manifest_from_dict(repository.get("baseline_manifest"))
        resolved_checks = _resolved_checks_from_list(payload.get("resolved_checks"))
        context_excerpts = _context_excerpts_from_list(
            payload.get("context_excerpts", ()),
            legacy_missing_reason=prompt_compiler_version == 1,
        )
        context_files = _context_files_from_list(payload.get("context_files", ()))
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
        context_files=context_files,
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
                "context_file_path": excerpt.context_file_path,
            }
            for excerpt in contract.context_excerpts
        ],
        "context_files": [
            {
                "workspace_path": context_file.workspace_path,
                "content": context_file.content,
                "sha256": context_file.sha256,
                "byte_count": context_file.byte_count,
                "required": context_file.required,
                "reason": context_file.reason,
                "role": context_file.role,
                "refs": list(context_file.refs),
                "source_paths": list(context_file.source_paths),
            }
            for context_file in contract.context_files
        ],
    }


def _resolve_checks(
    project_root: Path,
    contract: ImplementationContractDraft,
    transition: TransitionDefinition,
) -> tuple[tuple[ResolvedCheck, ...], tuple[DomainError, ...]]:
    """LEGACY resolver: focus checks from a per-task artifact and the transition's
    validations. The global command path uses :func:`_global_command_check`
    instead and never consults acceptance profiles, invariants, or file surfaces."""
    checks: dict[str, ResolvedCheck] = {}
    errors: list[DomainError] = []
    for check in contract.focused_checks:
        checks[check.id] = _focused_check(check)

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
    return (
        tuple(checks[key] for key in sorted(checks)),
        tuple(errors),
    )


def _focused_check(check: ContractCheck) -> ResolvedCheck:
    return ResolvedCheck(
        id=check.id,
        source="task",
        runner="command",
        argv=check.argv,
        timeout_seconds=check.timeout_seconds,
        expect=check.expect,
    )


CONTEXT_EXCERPT_CHARACTER_LIMIT = 1_200
CONTEXT_EXCERPTS_TOTAL_CHARACTER_LIMIT = 2_000


def _freeze_linked_context(
    project_root: Path,
    task: Task,
    transition: TransitionDefinition,
    *,
    parent_tasks: tuple[Task, ...] = (),
) -> tuple[
    tuple[FrozenContextExcerpt, ...],
    tuple[FrozenContextFile, ...],
    tuple[DomainError, ...],
]:
    """Resolve and freeze the task's linked reference context at job creation.

    Uses the same resolver and role rendering as the legacy render route so both
    paths consume one shared resolution output: the source idea/specification,
    its canonical answer lineage, and relevant linked references. Required links,
    ambiguous links, answer conflicts, and containment are verified before a
    worker is admitted.

    The complete source bytes are frozen with provenance so a worker can always
    read the full required document from the workspace, not just a clipped
    inline excerpt. Identical content is stored once while all logical
    references are retained. Oversized references are clipped in the inline
    excerpt with an explicit marker pointing at the full frozen workspace file,
    never dropped silently.
    """
    resolver = LinkedContextResolver(project_root)
    result = resolver.build_context_packet(
        task_for_context(task, transition),
        parent_tasks=parent_tasks,
    )
    if not result.accepted:
        return (), (), result.errors
    packet = result.packet
    references = packet.references
    documents = packet.documents
    frozen_files: list[FrozenContextFile] = []
    frozen_excerpts: list[FrozenContextExcerpt] = []
    total_characters = 0
    seen_excerpt_hashes: set[str] = set()
    for document in documents:
        workspace_path = _context_workspace_file_path(document.sha256)
        refs = tuple(references.get(document.sha256, (document.ref,))) or (document.ref,)
        required = _context_document_required(document)
        reason = _context_role_reason(document)
        role = _context_document_role(document)
        frozen_files.append(FrozenContextFile(
            workspace_path=workspace_path,
            content=document.content,
            sha256=document.sha256,
            byte_count=len(document.content.encode("utf-8")),
            required=required,
            reason=reason,
            role=role,
            refs=refs,
            source_paths=(str(document.path),),
        ))
        text = render_context_document(document)
        if len(text) > CONTEXT_EXCERPT_CHARACTER_LIMIT:
            text = _clip_context_document(
                text,
                CONTEXT_EXCERPT_CHARACTER_LIMIT,
                workspace_path=workspace_path,
            )
        remaining = CONTEXT_EXCERPTS_TOTAL_CHARACTER_LIMIT - total_characters
        if len(text) > remaining:
            text = _clip_context_document(
                text,
                max(0, remaining),
                workspace_path=workspace_path,
            )
            if not text:
                break
        excerpt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if excerpt_hash in seen_excerpt_hashes:
            continue
        seen_excerpt_hashes.add(excerpt_hash)
        total_characters += len(text)
        frozen_excerpts.append(FrozenContextExcerpt(
            artifact=document.ref,
            heading=_context_role_heading(document),
            reason=reason,
            text=text,
            sha256=excerpt_hash,
            context_file_path=workspace_path,
        ))
    return tuple(frozen_excerpts), tuple(frozen_files), ()


def _clip_context_document(text: str, limit: int, *, workspace_path: str) -> str:
    marker = (
        "\n[reference clipped at resolution budget; full source is available "
        f"at workspace file {workspace_path}]"
    )
    if limit <= len(marker):
        return marker[:limit]
    return text[: limit - len(marker)].rstrip() + marker


def _context_role_heading(document) -> str:
    if document.is_canonical_question_round_answers:
        return (
            "Canonical QuestionRound Answers"
            if document.is_current_question_round_answers
            else "Canonical QuestionRound Answer History"
        )
    if document.is_execution_contract:
        return "Generated Execution Contract"
    return "Linked Reference Context"


def _context_role_reason(document) -> str:
    if document.is_canonical_question_round_answers:
        return "Settled authoritative answers the task must not re-ask or contradict."
    if document.is_execution_contract:
        return "Boundary and interface reference the task must satisfy."
    return "Specific linked reference material applicable to this task."


def _context_document_required(document) -> bool:
    if (
        document.is_canonical_question_round_answers
        or document.is_execution_contract
    ):
        return True
    return bool(document.required)


def _context_document_role(document) -> str:
    if document.is_canonical_question_round_answers:
        return "answer"
    if document.is_execution_contract:
        return "implementation_contract"
    return "reference"


def _context_workspace_file_path(sha256: str) -> str:
    """Stable relative workspace path for one deduplicated frozen source file.

    Paths always live under the internal ``.open-tulid/context/`` area so they
    can never be promoted to the application repository.
    """
    return f"context/{sha256[:12]}.md"


def _freeze_context_excerpts(project_root: Path, task: Task, selections) -> tuple[tuple, tuple[DomainError, ...]]:
    frozen: list = []
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


def _global_draft_from_dict(raw: object) -> ImplementationContractDraft:
    payload = _mapping(raw, "generated_contract")
    source = _mapping(payload.get("source"), "source")
    surface_payload = _mapping(payload.get("change_surface"), "change_surface")
    checks_payload = _mapping(payload.get("checks"), "checks")
    return ImplementationContractDraft(
        schema=_required_string(payload, "schema"),
        source_task_id=_required_string(source, "task_id"),
        source_intent_sha256=_required_string(source, "source_intent_sha256"),
        profile=_required_string(payload, "profile"),
        objective=_required_string(payload, "objective"),
        change_surface=ChangeSurface(
            add=_string_tuple(surface_payload.get("add")),
            edit=_string_tuple(surface_payload.get("edit")),
            forbidden=_string_tuple(surface_payload.get("forbidden")),
            max_files=_optional_int_value(surface_payload.get("max_files")),
            max_changed_lines=_optional_int_value(surface_payload.get("max_changed_lines")),
        ),
        requirements=_string_tuple(payload.get("requirements")),
        focused_checks=(),
        invariants=_string_tuple(checks_payload.get("invariants")),
        acceptance_profiles=_string_tuple(checks_payload.get("profiles")),
        vertical_slice_exemption=_optional_string_value(checks_payload.get("vertical_slice_exemption")),
        interfaces=(),
        failure_behavior=_string_tuple(payload.get("failure_behavior")),
        non_goals=_string_tuple(payload.get("non_goals")),
        context_excerpts=(),
    )


def _implementation_contract_from_dict(raw: object) -> ImplementationContractDraft:
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
) -> tuple:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("context_excerpts must be a list")
    excerpts = []
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
        context_file_path = payload.get("context_file_path")
        if context_file_path is not None and (
            not isinstance(context_file_path, str) or not context_file_path
        ):
            raise ValueError("context_file_path must be a non-empty string or null")
        excerpts.append(FrozenContextExcerpt(
            artifact=_required_string(payload, "artifact"),
            heading=_required_string(payload, "heading"),
            reason=reason,
            text=text,
            sha256=sha256,
            context_file_path=context_file_path,
        ))
    return tuple(excerpts)


def _context_files_from_list(raw: object) -> tuple:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("context_files must be a list")
    files: list[FrozenContextFile] = []
    for item in raw:
        payload = _mapping(item, "context_files[]")
        content = _required_string(payload, "content")
        sha256 = _required_string(payload, "sha256")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != sha256:
            raise ValueError("context file hash mismatch")
        byte_count = payload.get("byte_count", 0)
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise ValueError("context file byte_count must be an integer")
        if byte_count != len(content.encode("utf-8")):
            raise ValueError("context file byte_count mismatch")
        required = payload.get("required", False)
        if not isinstance(required, bool):
            raise ValueError("context file required must be a boolean")
        reason = _required_string(payload, "reason")
        role = str(payload.get("role", "reference")) or "reference"
        workspace_path = _required_string(payload, "workspace_path")
        workspace = Path(workspace_path)
        if (
            workspace.is_absolute()
            or ".." in workspace.parts
            or not workspace.parts
            or workspace.parts[0] != "context"
        ):
            raise ValueError("context file workspace_path must be a safe context path")
        files.append(FrozenContextFile(
            workspace_path=workspace_path,
            content=content,
            sha256=sha256,
            byte_count=byte_count,
            required=required,
            reason=reason,
            role=role,
            refs=_string_tuple(payload.get("refs")),
            source_paths=_string_tuple(payload.get("source_paths")),
        ))
    return tuple(files)


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


def _optional_int_value(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("value must be an integer or null")
    return raw


def _optional_string_value(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw or None
    raise ValueError("value must be a string or null")


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
