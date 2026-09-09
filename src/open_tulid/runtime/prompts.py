"""Deterministic model-facing prompt packets for frozen execution contracts."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from open_tulid.domain import DomainError, ExecutionJob

from .execution_contracts import (
    ExecutionContract,
    execution_contract_to_dict,
    load_job_execution_contract,
)
from .prompt_versions import PROMPT_COMPILER_VERSION
from .verifier import VERIFICATION_REPORT_SCHEMA, VERIFICATION_REPORT_SCHEMA_V2

# Inline prompt capacity is bounded in characters; workspace reference capacity
# is bounded only by the frozen byte/bundle limits and is separate. Characters
# are never labelled as tokens: an exact token count is not claimed unless a
# provider advertises a usable token estimator.
TOTAL_BUDGET = 6000
SECTION_BUDGETS = {
    "repository_facts": 300,
    "prior_implementation_evidence": 2000,
    "completion_submission": 900,
}
# Only background context is trimmed before the mandatory sections. The
# assigned task, verification commands, procedure, and completion protocol
# never truncate. Required context files stay complete in the workspace bundle
# even when only their reading instructions fit inline.
_OPTIONAL_TRIM_ORDER = (
    "repository_facts",
)
_UNRESOLVED_MARKER_RE = re.compile(r"\{\{[^{}]+\}\}|<TODO>|<TBD>|\bFIXME_PROMPT\b")
_READING_PATH_RE = re.compile(r"\.open-tulid/(context/[0-9a-f]{12}\.md)")

# A fenced shell block is Tulid's single completion mechanism. Any second shell
# block is a forbidden task-local command block that must not be emitted.
_SHELL_FENCE_RE = re.compile(r"```(?:sh|bash)\s*\n", re.IGNORECASE)

# Preview renders through the same resolver/compiler as scheduling but under a
# synthetic job identity and without mutating scheduler state. Its substantive
# sections must match a real scheduled packet byte-for-byte from identical
# inputs. The only literal ephemeral field that still varies between a preview
# and a scheduled packet is the legacy ``Job: <id>`` header; the completion
# endpoint and token are already emitted as ``$OPEN_TULID_COMPLETION_ENDPOINT``
# and ``$OPEN_TULID_COMPLETION_TOKEN`` placeholders in every route.
PREVIEW_JOB_ID = "PROMPT_PREVIEW"
_EPHEMERAL_JOB_HEADER_RE = re.compile(r"(?m)^Job:\s+\S+$")


class PromptBudgetError(ValueError):
    """Raised when the full task or a mandatory instruction block cannot fit.

    ``code`` names the failure so callers can surface a precise, non-truncated
    diagnostic instead of a generic compile failure.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
_COMPILER_OWNED_SOURCE_KINDS = frozenset({
    "repository_facts",
    "runtime",
    "runtime_policy",
})


@dataclass(frozen=True)
class PromptSection:
    id: str
    heading: str
    text: str
    source_kind: str
    source_ref: str
    selection_reason: str
    budget: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class PromptManifest:
    compiler_version: int
    packet_type: str
    execution_contract_sha256: str
    sections: tuple[Mapping[str, object], ...]
    packet_sha256: str
    characters: int
    character_budget: int
    # Reasons for every optional context/tail truncated or dropped so the
    # packet's serialization and preview explain what was omitted and why.
    optional_omissions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "compiler_version": self.compiler_version,
            "packet_type": self.packet_type,
            "execution_contract_sha256": self.execution_contract_sha256,
            "sections": [dict(section) for section in self.sections],
            "packet_sha256": self.packet_sha256,
            "characters": self.characters,
            "character_budget": self.character_budget,
            "optional_omissions": list(self.optional_omissions),
        }


@dataclass(frozen=True)
class CompiledPrompt:
    text: str
    sections: tuple[PromptSection, ...]
    manifest: PromptManifest


@dataclass(frozen=True)
class ReviewEvidence:
    source_job_id: str
    verification_report: Mapping[str, object]
    repair_history: tuple[Mapping[str, object], ...] = ()


def compile_execution_prompt(
    contract: ExecutionContract,
    *,
    review_evidence: ReviewEvidence | None = None,
) -> CompiledPrompt:
    """Compile only frozen inputs; never consult live tracker or repository files."""
    if is_review_transition(contract.transition):
        if review_evidence is None:
            raise ValueError(
                "self-review requires immutable verification evidence from the prior implementation job"
            )
        return _compile_review_prompt(contract, review_evidence)
    return _compile_implementation_prompt(contract)


def find_review_evidence(
    jobs: Sequence[ExecutionJob],
    *,
    project_id: str,
    task_id: str,
    review_transition: object,
    current_contract=None,
    workflow=None,
    journals=None,
) -> ReviewEvidence | None:
    """Select the newest accepted job that produced the review transition's source state."""
    from_state = str(getattr(review_transition, "from_state", ""))
    candidates: list[ExecutionJob] = []
    for job in jobs:
        if current_contract is not None:
            from .acceptance import accepted_task_evidence
            from .execution_contracts import source_content_identities
            if not accepted_task_evidence(
                job, task=current_contract.source_task, workflow=workflow, journals=journals,
                source_identities=source_content_identities(current_contract),
            ):
                continue
        status = job.status.value if hasattr(job.status, "value") else str(job.status)
        frozen = load_job_execution_contract(job, required=True)
        report = job.metadata.get("verification_report")
        if (
            job.project_id == project_id
            and job.task_id == task_id
            and status == "accepted"
            and frozen.accepted
            and frozen.contract is not None
            and frozen.contract.transition.to_state == from_state
            and isinstance(report, Mapping)
            and report.get("schema") in {VERIFICATION_REPORT_SCHEMA, VERIFICATION_REPORT_SCHEMA_V2}
            and report.get("baseline_sha256")
            == frozen.contract.baseline_manifest.sha256
        ):
            candidates.append(job)
    if not candidates:
        return None
    source = max(
        candidates,
        key=lambda job: (str(job.metadata.get("created_at", "")), job.job_id),
    )
    raw_history = source.metadata.get("repair_history", ())
    history = tuple(
        dict(item) for item in raw_history
        if isinstance(item, Mapping)
    ) if isinstance(raw_history, Sequence) and not isinstance(raw_history, (str, bytes)) else ()
    return ReviewEvidence(
        source_job_id=source.job_id,
        verification_report=dict(source.metadata["verification_report"]),
        repair_history=history,
    )


def normalize_ephemeral_completion_fields(
    packet: str,
    *,
    job_id: str = PREVIEW_JOB_ID,
) -> str:
    """Canonicalize the only ephemeral fields that differ preview vs. a packet.

    A preview compiles the same frozen inputs as a scheduled job with a
    synthetic job identity and no scheduler mutation, so comparing the two
    requires normalizing only the fields that legitimately vary per instance.
    The completion endpoint and token are already emitted as env placeholders,
    so the single remaining literal is the legacy ``Job: <id>`` header. All
    other sections must be byte-for-byte identical or the packet is not a
    faithful preview of scheduling.
    """
    return _EPHEMERAL_JOB_HEADER_RE.sub(f"Job: {job_id}", packet)


def lint_compiled_prompt(
    compiled: CompiledPrompt,
    *,
    contract: ExecutionContract | None = None,
) -> tuple[DomainError, ...]:
    """Return stable structural diagnostics for a compiled or historical packet."""
    errors: list[DomainError] = []
    ids = [section.id for section in compiled.sections]
    for duplicate in _duplicates(ids):
        errors.append(_lint_error(
            "prompt.duplicate_section",
            f"Singleton prompt section appears more than once: {duplicate}",
            duplicate,
        ))
    normalized: dict[str, str] = {}
    for section in compiled.sections:
        block = " ".join(section.text.split()).casefold()
        if block and block in normalized:
            errors.append(_lint_error(
                "prompt.duplicate_block",
                f"Prompt sections {normalized[block]!r} and {section.id!r} contain the same normalized text.",
                section.id,
            ))
        normalized[block] = section.id
        if (
            section.source_kind in _COMPILER_OWNED_SOURCE_KINDS
            and _UNRESOLVED_MARKER_RE.search(section.text)
        ):
            errors.append(_lint_error(
                "prompt.unresolved_marker",
                f"Prompt section {section.id!r} contains an unresolved template marker.",
                section.id,
            ))
    actual_hash = hashlib.sha256(compiled.text.encode("utf-8")).hexdigest()
    if actual_hash != compiled.manifest.packet_sha256:
        errors.append(_lint_error(
            "prompt.packet_hash_mismatch",
            "Prompt text does not match its manifest packet hash.",
        ))
    if len(compiled.text) > compiled.manifest.character_budget:
        errors.append(_lint_error(
            "prompt.total_budget_exceeded",
            "Prompt text exceeds its declared character budget.",
        ))
    if compiled.text.count("curl -sS -X POST") != 1:
        errors.append(_lint_error(
            "prompt.completion_count",
            "Prompt must contain exactly one completion submission command.",
            "completion_submission",
        ))
    validation = next(
        (section.text for section in compiled.sections if section.id == "required_validation"),
        "",
    )
    validation_lines = tuple(
        " ".join(line.split())
        for line in validation.splitlines()
        if line.strip().startswith("- ")
    )
    for duplicate in _duplicates(validation_lines):
        errors.append(_lint_error(
            "prompt.validation_duplicate",
            f"Required validation appears more than once: {duplicate}",
            "required_validation",
        ))
    if contract is not None:
        if compiled.manifest.execution_contract_sha256 != contract.sha256:
            errors.append(_lint_error(
                "prompt.contract_hash_mismatch",
                "Prompt manifest does not match the frozen execution contract.",
            ))
        for check in contract.resolved_checks:
            command = shlex.join(check.argv) if check.argv else ""
            if validation.count(f"- {check.id}:") != 1 or (command and validation.count(command) < 1):
                errors.append(_lint_error(
                    "prompt.validation_mismatch",
                    f"Resolved validation {check.id!r} is not represented exactly once.",
                    check.id,
                ))
        for excerpt in contract.context_excerpts:
            if hashlib.sha256(excerpt.text.encode("utf-8")).hexdigest() != excerpt.sha256:
                errors.append(_lint_error(
                    "prompt.context_hash_mismatch",
                    f"Selected context excerpt {excerpt.artifact!r} failed its integrity check.",
                    excerpt.artifact,
                ))
        leaked_hashes = tuple(
            identity
            for identity in _audit_hashes(contract)
            if identity in compiled.text
        )
        if leaked_hashes:
            errors.append(_lint_error(
                "prompt.audit_metadata_leak",
                "Prompt model text contains frozen audit metadata.",
            ))
        # The authoritative task body must appear exactly once and never be
        # duplicated under another synthetic section.
        body = contract.source_task.body.strip()
        if body:
            containing = [section.id for section in compiled.sections if body in section.text]
            if len(containing) > 1:
                errors.append(_lint_error(
                    "prompt.duplicate_task_content",
                    "Authoritative task body appears in more than one section: "
                    f"{', '.join(dict.fromkeys(containing))}.",
                    "assigned_task",
                ))
        # Every required source must be named in required reading, and every
        # named reading path must resolve to a frozen workspace file.
        reading = next(
            (section.text for section in compiled.sections if section.id == "required_reading"),
            "",
        )
        named_paths = set(_reading_context_paths(reading))
        frozen_paths = {context_file.workspace_path for context_file in contract.context_files}
        missing_paths = tuple(
            context_file.workspace_path
            for context_file in contract.context_files
            if context_file.required and context_file.workspace_path not in named_paths
        )
        for path in missing_paths:
            errors.append(_lint_error(
                "prompt.missing_required_source",
                f"Required reading omits frozen source file `{path}`.",
                "required_reading",
            ))
        for ref in sorted(named_paths - frozen_paths):
            errors.append(_lint_error(
                "prompt.unresolved_reading_path",
                f"Required reading names workspace file `{ref}` with no matching "
                "frozen source.",
                "required_reading",
            ))
        # Only the single completion shell block is permitted; any additional
        # shell block is a forbidden task-local command block.
        shell_blocks = len(tuple(_SHELL_FENCE_RE.finditer(compiled.text)))
        if shell_blocks != 1:
            errors.append(_lint_error(
                "prompt.forbidden_command_block",
                f"Expected exactly one shell command block (completion example); "
                f"found {shell_blocks}.",
                "completion_submission",
            ))
    return tuple(errors)


def compiled_prompt_from_metadata(metadata: Mapping[str, object]) -> CompiledPrompt:
    """Load and integrity-check an immutable packet stored with an execution job."""
    text = metadata.get("prompt_packet")
    manifest_raw = metadata.get("prompt_manifest")
    expected = metadata.get("prompt_packet_sha256")
    if not isinstance(text, str) or not isinstance(manifest_raw, Mapping) or not isinstance(expected, str):
        raise ValueError("job has no complete immutable prompt packet")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
        raise ValueError("historical prompt packet failed its integrity check")
    if manifest_raw.get("packet_sha256") != expected:
        raise ValueError("historical prompt manifest packet hash does not match job metadata")
    if manifest_raw.get("characters", len(text)) != len(text):
        raise ValueError("historical prompt manifest character count is invalid")
    contract_hash = metadata.get("execution_contract_sha256")
    if (
        isinstance(contract_hash, str)
        and manifest_raw.get("execution_contract_sha256") != contract_hash
    ):
        raise ValueError("historical prompt manifest does not match its execution contract")
    raw_sections = manifest_raw.get("sections")
    if not isinstance(raw_sections, Sequence) or isinstance(raw_sections, (str, bytes)):
        raise ValueError("historical prompt manifest has no section list")
    rendered_sections = _parse_rendered_sections(text, raw_sections)
    if len(rendered_sections) != len(raw_sections):
        raise ValueError("historical prompt section count does not match its manifest")
    sections: list[PromptSection] = []
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, Mapping):
            raise ValueError("historical prompt manifest contains an invalid section")
        section_id = _manifest_string(raw, "id")
        heading = _manifest_string(raw, "heading")
        if index >= len(rendered_sections) or rendered_sections[index][0] != heading:
            raise ValueError("historical prompt section order does not match its manifest")
        section_text = rendered_sections[index][1]
        if hashlib.sha256(section_text.encode("utf-8")).hexdigest() != raw.get("sha256"):
            raise ValueError("historical prompt section failed its integrity check")
        sections.append(PromptSection(
            id=section_id,
            heading=heading,
            text=section_text,
            source_kind=str(raw.get("source_kind", "historical")),
            source_ref=str(raw.get("source_ref", section_id)),
            selection_reason=str(raw.get("selection_reason", "Persisted executed packet")),
            budget=int(raw["budget"]) if isinstance(raw.get("budget"), int) else None,
            truncated=bool(raw.get("truncated", False)),
        ))
    manifest = PromptManifest(
        compiler_version=_manifest_int(manifest_raw, "compiler_version", minimum=1),
        packet_type=_manifest_string(manifest_raw, "packet_type"),
        execution_contract_sha256=_manifest_string(
            manifest_raw, "execution_contract_sha256"
        ),
        sections=tuple(dict(item) for item in raw_sections if isinstance(item, Mapping)),
        packet_sha256=_manifest_string(manifest_raw, "packet_sha256"),
        characters=_manifest_int(manifest_raw, "characters", minimum=0),
        character_budget=_manifest_int(
            manifest_raw,
            "character_budget",
            minimum=1,
        ),
        optional_omissions=_manifest_omissions(manifest_raw.get("optional_omissions")),
    )
    compiled = CompiledPrompt(text=text, sections=tuple(sections), manifest=manifest)
    issues = lint_compiled_prompt(compiled)
    fatal = tuple(issue for issue in issues if issue.code.endswith("hash_mismatch"))
    if fatal:
        raise ValueError(fatal[0].message)
    return compiled


def _compile_implementation_prompt(contract: ExecutionContract) -> CompiledPrompt:
    sections = (
        # 1. Assigned outcome and complete task requirements (one authoritative
        #    section; the task body is never repeated under another heading).
        _section(
            "assigned_task", "Assigned Task",
            _assigned_task_text(contract),
            "execution_contract", "generated_contract",
            "States the full assigned task and its in-scope requirements once.",
        ),
        # 2. Required reading with exact workspace paths and why each file matters.
        _section(
            "required_reading", "Required Reading",
            _required_reading_text(contract),
            "context_file", _context_reading_refs(contract),
            "Names every complete frozen source file the worker can and must read.",
        ),
        # 3. Prerequisite interfaces/results and relevant repository facts.
        _section(
            "repository_facts", "Repository Facts",
            _repository_facts_text(contract.repository_facts),
            "repository_facts", "repository.facts",
            "Provides observed toolchain, entrypoint facts, and prerequisite seams.",
            truncatable=True,
        ),
        # 4. Applicable worker procedure and global verification commands.
        _section(
            "execution_procedure", "Execution Procedure", "\n".join((
                "1. Read the assigned task, its requirements, and the required reading.",
                "2. Respect the settled answer precedence stated in any answer reference.",
                "3. Inspect the workspace and integration seams before editing.",
                "4. Make the smallest coherent implementation for this task.",
                "5. Run every required project verification command locally.",
                "6. Fix failures inside the task boundary; do not chase unrelated or environmental failures.",
                "7. Submit completion evidence, or stop on an out-of-scope, baseline, or environment blocker.",
            )),
            "runtime_policy", f"compiler/v{PROMPT_COMPILER_VERSION}",
            "Provides one implementation-model inspect-implement-test-submit loop.",
        ),
        _section(
            "required_validation", "Required Validation", _checks_text(contract.resolved_checks),
            "execution_contract", "resolved_checks", "Lists the exact checks Tulid will independently run.",
        ),
        # 5. Completion submission and repair protocol.
        _section(
            "completion_submission", "Completion Submission", _completion_text(contract),
            "runtime", "completion_api", "Provides the sole completion mechanism.",
        ),
    )
    return _finalize(contract, "implementation", sections)


def _assigned_task_text(contract: ExecutionContract) -> str:
    """Render the one authoritative task section for an implementation prompt.

    The full task body appears here and nowhere else in the packet. In-scope
    requirements, failure behavior, and non-goals are rendered alongside it so a
    worker does not need to reconstruct decisions already made during planning.
    """
    generated = contract.generated_contract
    lines = [
        contract.source_task.body.strip() or generated.objective,
        "\nImplement only this scoped task. Do not broaden the change surface.",
        (
            "Acceptance: the project global verification commands below must pass. "
            "There is no file/directory allowlist; you may create, edit, rename, "
            "or delete any files the task requires."
        ),
    ]
    if generated.requirements:
        lines.append("Requirements:")
        lines.extend(f"- {item}" for item in generated.requirements)
    if generated.failure_behavior:
        lines.append("Failure behavior:")
        lines.extend(f"- {item}" for item in generated.failure_behavior)
    if generated.non_goals:
        lines.append("Non-goals:")
        lines.extend(f"- {item}" for item in generated.non_goals)
    return "\n".join(lines)


def _required_reading_text(
    contract: ExecutionContract,
    *,
    max_chars: int | None = None,
) -> str:
    """Name every frozen source file with its exact workspace path and purpose.

    Required files are always named and stay complete in the frozen bundle; only
    the inline excerpt is bounded by ``max_chars``. When a bounded excerpt set
    cannot fit inline, the omitted excerpts are skipped with an explicit marker
    and the reading instructions still point at the complete workspace files.
    """
    files = tuple(contract.context_files)
    excerpts = tuple(contract.context_excerpts)
    chunks: list[str] = []
    if files:
        chunks.append(
            "Read the complete frozen source files below from your workspace "
            "(paths are relative to the workspace root). Required files are "
            "mandatory reading; optional files are background context. The "
            "complete bytes are present even when only a relevant excerpt is "
            "quoted below."
        )
        listing = []
        for context_file in files:
            kind = "required" if context_file.required else "optional background"
            role = context_file.role or "reference"
            listing.append(
                f"- `.open-tulid/{context_file.workspace_path}` — {kind} "
                f"{role}: {context_file.reason}"
            )
        chunks.append("\n".join(listing))
    if excerpts and files:
        chunks.append(
            "Relevant excerpts follow. When you need the full document, read "
            "the matching frozen file listed above instead of relying on the "
            "inline clip."
        )
    elif not excerpts:
        chunks.append("No additional context excerpts were selected for this job.")

    marker = (
        "\n\n[additional context excerpts omitted to fit the inline prompt "
        "budget; the complete required sources remain at their workspace "
        "files listed above]"
    )

    def _bounded() -> tuple[list[str], int]:
        kept_parts = list(chunks)
        dropped_count = 0
        reserved = len(marker) if max_chars is not None else 0
        for excerpt in excerpts:
            block = f"### {excerpt.artifact}: {excerpt.heading}\n\n{excerpt.text}"
            candidate = kept_parts + [block]
            candidate_len = len("\n\n".join(candidate))
            if max_chars is not None and candidate_len + reserved > max_chars:
                dropped_count += 1
                continue
            kept_parts.append(block)
        return kept_parts, dropped_count

    parts, dropped = _bounded()
    text = "\n\n".join(parts)
    if dropped:
        text = text + marker
    return text


def _repository_facts_text(facts: RepositoryFacts) -> str:
    return "\n".join(filter(None, (
        "Repository is available." if facts.repository_available else "No repository is available.",
        "Top-level: " + ", ".join(facts.top_level_entries),
        "Manifests: " + ", ".join(facts.manifests),
        "Entrypoints: " + ", ".join(facts.detected_entrypoints),
        "Prerequisite interfaces/results live in the required reading above; inspect "
        "those files and the named seams before editing.",
    )))


def _context_reading_refs(contract: ExecutionContract) -> str:
    refs = tuple(f".open-tulid/{context_file.workspace_path}" for context_file in contract.context_files)
    return ", ".join(refs) or "none"


def _compile_review_prompt(
    contract: ExecutionContract,
    evidence: ReviewEvidence,
) -> CompiledPrompt:
    report = evidence.verification_report
    changes = report.get("changes", {})
    checks = report.get("checks", ())
    prior = {
        "source_job_id": evidence.source_job_id,
        "changes": changes if isinstance(changes, Mapping) else {},
        "checks": checks if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)) else [],
        "repair_history": list(evidence.repair_history),
    }
    sections = (
        _section(
            "mission", "Review Mission",
            "\n".join((
                f"Audit the implementation produced for task {contract.source_task.title!r} "
                "against its requirements and the authoritative prior implementation evidence below.",
                "Find a concrete in-scope defect and correct it, or submit a valid no-change review.",
                "Do not restart implementation or perform unrelated cleanup.",
            )),
            "execution_contract", "generated_contract.objective",
            "Frames self-review as an evidence-backed, requirement-driven audit rather than another implementation pass.",
            truncatable=True,
        ),
        _section(
            "assigned_task", "Assigned Task and Requirements",
            _contract_text(contract, include_requirements=True, include_objective=True),
            "execution_contract", "generated_contract",
            "States the authoritative task body and every requirement review must map to code or test evidence.",
        ),
        _section(
            "required_reading", "Required Reading",
            _required_reading_text(contract),
            "context_file", _context_reading_refs(contract),
            "Names every complete frozen source file (task, specification, canonical answers) review may read.",
            truncatable=True,
        ),
        _section(
            "prior_implementation_evidence", "Prior Implementation Evidence",
            _review_evidence_text(prior),
            "historical_job", evidence.source_job_id,
            "Supplies the authoritative prior diff summary, checks, repair history, and an explicit no-evidence marker.",
        ),
        _section(
            "required_validation", "Required Validation", _checks_text(contract.resolved_checks),
            "execution_contract", "resolved_checks",
            "Lists fresh checks required after review, including a no-change review.",
        ),
        _section(
            "review_procedure", "Review Procedure", "\n".join((
                "1. Re-read the assigned task, then carry every requirement and settled answer into your working notes.",
                "2. For each requirement, map it to code and tests: does the changed code satisfy the behavior, and does a test assert it?",
                "3. Examine the relevant integration seams (within and across the named interfaces, persistence, and stages), not only the changed files.",
                "4. Inspect beyond the changed files wherever a concrete requirement or prior failure points to locating the behavior.",
                "5. Identify missing behavior, ineffective/brittle tests, regressions, scope drift, and unfinished user-facing states.",
                "6. Make only a small targeted correction inside the task boundary; otherwise leave the workspace unchanged.",
                "7. Run the narrowest affected tests, then every required project check.",
                "8. Submit the correction or an explicit no-change review with fresh evidence and a compact review result.",
            )),
            "runtime_policy", f"compiler/v{PROMPT_COMPILER_VERSION}",
            "Provides the distinct requirement-to-evidence review loop.",
        ),
        _section(
            "scope_reminder", "Review Boundaries",
            "Do not add abstractions, broaden tests, create planning reports, or edit unrelated files. "
            "An empty diff is correct when no concrete defect exists. A corrective patch must stay "
            "inside the assigned task, pass the same global policy, and be promoted as a new verified candidate.",
            "runtime_policy", f"compiler/v{PROMPT_COMPILER_VERSION}",
            "Prevents gratuitous review edits, broad cleanup, and out-of-scope redesign.",
        ),
        _section(
            "review_result", "Review Result",
            _review_result_guidance_text(),
            "runtime", "review_result",
            "Requests the compact requirement-to-evidence result retained in the completion record.",
        ),
        _section(
            "completion_submission", "Completion Submission", _completion_text(contract, review_result=True),
            "runtime", "completion_api", "Provides the sole completion mechanism, including the review result.",
        ),
    )
    return _finalize(contract, "self_review", sections)


def _section(
    id: str,
    heading: str,
    text: str,
    source_kind: str,
    source_ref: str,
    selection_reason: str,
    *,
    truncatable: bool = False,
) -> PromptSection:
    clean = text.strip()
    budget = SECTION_BUDGETS.get(id)
    if budget is not None and len(clean) > budget:
        if not truncatable:
            raise ValueError(f"prompt section {id!r} exceeds its {budget}-character budget")
        clean = _truncate(clean, budget)
        return PromptSection(
            id, heading, clean, source_kind, source_ref, selection_reason, budget, True
        )
    return PromptSection(
        id, heading, clean, source_kind, source_ref, selection_reason, budget, False
    )


def _finalize(
    contract: ExecutionContract,
    packet_type: str,
    sections: tuple[PromptSection, ...],
) -> CompiledPrompt:
    sections, omissions = _fit_total_budget(sections, contract)
    rendered = _render_sections(sections)
    packet_sha = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    manifest_sections = tuple({
        "id": section.id,
        "heading": section.heading,
        "source_kind": section.source_kind,
        "source_ref": section.source_ref,
        "selection_reason": section.selection_reason,
        "sha256": hashlib.sha256(section.text.encode("utf-8")).hexdigest(),
        "characters": len(section.text),
        "budget": section.budget,
        "truncated": section.truncated,
    } for section in sections)
    compiled = CompiledPrompt(
        text=rendered,
        sections=sections,
        manifest=PromptManifest(
            PROMPT_COMPILER_VERSION,
            packet_type,
            contract.sha256,
            manifest_sections,
            packet_sha,
            len(rendered),
            TOTAL_BUDGET,
            omissions,
        ),
    )
    lint = lint_compiled_prompt(compiled, contract=contract)
    if lint:
        raise ValueError(f"{lint[0].code}: {lint[0].message}")
    return compiled


def _render_sections(sections: tuple[PromptSection, ...]) -> str:
    return "\n\n".join(f"## {section.heading}\n\n{section.text}" for section in sections)


def _fit_total_budget(
    sections: tuple[PromptSection, ...],
    contract: ExecutionContract,
) -> tuple[tuple[PromptSection, ...], tuple[str, ...]]:
    """Fit the packet to the inline prompt budget without truncating mandatory work.

    Optional background is trimmed first, then inline context excerpts are
    dropped (their complete workspace files stay named) before a binding
    overflow raises a named budget error. The assigned task, verification
    commands, procedure, and completion protocol are never truncated.
    """
    mutable = list(sections)
    omissions: list[str] = []

    def overflow() -> int:
        return len(_render_sections(tuple(mutable))) - TOTAL_BUDGET

    # 1. Trim optional background sections first; excerpts are higher priority.
    for section_id in _OPTIONAL_TRIM_ORDER:
        count = overflow()
        if count <= 0:
            break
        index = next((i for i, item in enumerate(mutable) if item.id == section_id), None)
        if index is None:
            continue
        item = mutable[index]
        keep = max(80, len(item.text) - count)
        if keep < len(item.text):
            mutable[index] = _revise_section(
                item,
                _truncate(item.text, keep),
                truncated=True,
            )
            omissions.append(
                f"Optional background section {section_id!r} was trimmed to fit "
                "the inline prompt budget; its prerequisite seams remain named "
                "below."
            )

    # 2. Drop inline context excerpts, never the required file listing.
    count = overflow()
    if count > 0:
        index = next((i for i, item in enumerate(mutable) if item.id == "required_reading"), None)
        if index is not None:
            item = mutable[index]
            target = max(1, len(item.text) - count)
            bounded_text = _required_reading_text(contract, max_chars=target)
            if len(bounded_text) < len(item.text):
                mutable[index] = _revise_section(
                    item,
                    bounded_text,
                    truncated=True,
                )
                omissions.append(
                    "Inline context excerpts were omitted to fit the prompt "
                    "budget; required reading file paths stay complete and the "
                    "full sources remain in the frozen workspace bundle."
                )

    # 3. The full task and mandatory sections must fit inline on their own.
    count = overflow()
    if count > 0:
        raise PromptBudgetError(
            "prompt.budget_exceeded",
            (
                f"Binding prompt sections exceed the {TOTAL_BUDGET}-character inline "
                f"budget by {count} characters even after trimming all optional "
                "background and inline context excerpts. The full assigned task "
                "and mandatory procedure/completion sections must fit inline; "
                "split or refine the task before running a worker."
            ),
        )
    return tuple(mutable), tuple(omissions)


def _revise_section(section: PromptSection, text: str, *, truncated: bool) -> PromptSection:
    return PromptSection(
        section.id,
        section.heading,
        text,
        section.source_kind,
        section.source_ref,
        section.selection_reason,
        section.budget,
        truncated,
    )


def _contract_text(
    contract: ExecutionContract,
    *,
    include_requirements: bool = True,
    include_objective: bool = True,
) -> str:
    generated = contract.generated_contract
    lines = [f"Objective: {generated.objective}"] if include_objective else []
    lines.append(
        "Acceptance: the project global verification commands below must "
        "pass. There is no file/directory allowlist; you may create, edit, rename, "
        "or delete any files the task requires."
    )
    if include_requirements and generated.requirements:
        lines.append("Requirements:")
        lines.extend(f"- {item}" for item in generated.requirements)
    if generated.failure_behavior:
        lines.append("Failure behavior:")
        lines.extend(f"- {item}" for item in generated.failure_behavior)
    if generated.non_goals:
        lines.append("Non-goals:")
        lines.extend(f"- {item}" for item in generated.non_goals)
    return "\n".join(lines)


def _checks_text(checks: Iterable[object]) -> str:
    checks = tuple(checks)
    if not checks:
        return "No validation commands were resolved."
    lines = []
    for check in checks:
        check_id = str(getattr(check, "id"))
        argv = tuple(getattr(check, "argv", ()))
        command = shlex.join(argv) if argv else "(trusted validation implementation)"
        lines.append(f"- {check_id}: {command}")
    return "\n".join(lines)


def _excerpts_text(contract: ExecutionContract) -> str:
    if not contract.context_excerpts:
        return "No additional context excerpts were selected for this job."
    return "\n\n".join(
        f"### {excerpt.artifact}: {excerpt.heading}\n\n{excerpt.text}"
        for excerpt in contract.context_excerpts
    )


def _completion_text(contract: ExecutionContract, *, review_result: bool = False) -> str:
    validation_ids = [check.id for check in contract.resolved_checks]
    artifacts: list[object] = (
        [{"type": artifact, "path": "required-output-path"} for artifact in contract.transition.requires.artifacts]
    )
    evidence = {check_id: "command/result evidence" for check_id in validation_ids}
    changed_files = (
        ["replace-with-each-actual-changed-path"]
        if contract.transition.requires.changed_files_required
        else []
    )
    example = {
        "summary": "what changed or why review required no change",
        "artifacts": artifacts,
        "changed_files": changed_files,
        "validation_evidence": evidence,
    }
    if review_result:
        example["review_result"] = _review_result_example()
    changed_files_rule = (
        "This transition requires a change. Replace the changed_files example with every actual changed workspace path."
        if contract.transition.requires.changed_files_required
        else "An empty changed_files list is valid when review makes no correction; otherwise list every actual changed path."
    )
    return "\n".join((
        "Submit completion exactly once after the work and required checks are complete.",
        changed_files_rule,
        "```sh",
        "curl -sS -X POST \\",
        "  -H \"content-type: application/json\" \\",
        "  -H \"x-open-tulid-completion-token: $OPEN_TULID_COMPLETION_TOKEN\" \\",
        "  \"$OPEN_TULID_COMPLETION_ENDPOINT\" \\",
        "  --data-binary @- <<'JSON'",
        json.dumps(example, sort_keys=True),
        "JSON",
        "```",
    ))


def _review_result_example() -> dict[str, object]:
    """Compact result example a review worker must mirror and fill in.

    The retained review result names the behavior, the relevant source/test
    evidence inspected, any defects/fixes, and remaining product blockers. An
    empty ``defects_fixes`` and ``remaining_blockers`` is valid for a genuine
    no-defect review. A remaining product decision is a blocker for the existing
    clarification/planning path, never an improvised redesign inside review.
    """
    return {
        "behavior": "behavior required by the task",
        "evidence": "source/tests inspected for it",
        "defects_fixes": [
            {"defect": "in-scope defect", "fix": "targeted fix", "status": "fixed"}
        ],
        "remaining_blockers": [],
    }


def _review_result_guidance_text() -> str:
    return "\n".join((
        "Retain one compact requirement-to-evidence result in the completion record.",
        "For each required behavior, name it and cite the source and test evidence that satisfies it.",
        "Record every concrete in-scope defect and its targeted fix, or state that none was found.",
        "If a genuine product decision is unresolved, list it under remaining_blockers and stop: "
        "it must go to the existing clarification/planning path, not be redesigned inside review.",
        "Generic assurances without cited code/test evidence are not acceptable.",
    ))


def _review_evidence_text(prior: Mapping[str, object]) -> str:
    lines = [
        f"Source implementation job: {prior['source_job_id']}",
        "Authoritative changes: "
        + json.dumps(prior.get("changes", {}), sort_keys=True, separators=(",", ":")),
        "Trusted implementation checks:",
    ]
    checks = prior.get("checks", ())
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for item in checks:
            if not isinstance(item, Mapping):
                continue
            detail = {
                "id": item.get("id"),
                "status": item.get("status"),
                "exit_code": item.get("exit_code"),
                "argv": item.get("argv", ()),
                "stdout": str(item.get("stdout", ""))[:200],
                "stderr": str(item.get("stderr", ""))[:400],
            }
            lines.append("- " + json.dumps(detail, sort_keys=True, separators=(",", ":")))
        if not checks:
            lines.append(
                "No trusted checks are recorded for this task. This packet is not "
                "evidence of a verified implementation; do not treat an empty diff "
                "as if verification ran."
            )
    history = prior.get("repair_history", ())
    if isinstance(history, Sequence) and not isinstance(history, (str, bytes)) and history:
        lines.append("Prior repair history:")
        for item in history[-5:]:
            if not isinstance(item, Mapping):
                continue
            detail = {
                "classification": item.get("classification"),
                "error_codes": item.get("error_codes", ()),
                "repair_ready": item.get("repair_ready"),
            }
            lines.append("- " + json.dumps(detail, sort_keys=True, separators=(",", ":")))
    bounded: list[str] = []
    used = 0
    marker = "[additional prior evidence omitted by review-packet budget]"
    for line in lines:
        addition = len(line) + (1 if bounded else 0)
        if used + addition + len(marker) > SECTION_BUDGETS["prior_implementation_evidence"]:
            bounded.append(marker)
            break
        bounded.append(line)
        used += addition
    return "\n".join(bounded)


def is_review_transition(transition: object) -> bool:
    from open_tulid.domain.completion import is_review_transition as declared_review
    return declared_review(transition)

def _truncate(text: str, limit: int) -> str:
    marker = "\n[truncated by prompt budget]"
    if limit <= len(marker):
        return marker[-limit:]
    return text[: limit - len(marker)].rstrip() + marker


def _duplicates(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    repeated: list[str] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return tuple(repeated)


def _lint_error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _reading_context_paths(text: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in _READING_PATH_RE.finditer(text))


def _manifest_omissions(raw: object) -> tuple[str, ...]:
    """Parse the optional_omissions manifest field, defaulting to empty."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("optional_omissions must be a list of strings")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("optional_omissions entries must be strings")
        values.append(item)
    return tuple(values)


def _manifest_string(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"prompt manifest field {key!r} must be a non-empty string")
    return value


def _manifest_int(
    raw: Mapping[str, object],
    key: str,
    *,
    minimum: int,
) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(
            f"prompt manifest field {key!r} must be an integer >= {minimum}"
        )
    return value


def _audit_hashes(contract: ExecutionContract) -> frozenset[str]:
    identities: set[str] = set()

    def collect(value: object, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                collect(child_value, str(child_key))
        elif (
            key.endswith("sha256")
            and isinstance(value, str)
            and len(value) == 64
        ):
            identities.add(value)

    collect(execution_contract_to_dict(contract))
    return frozenset(identities)


def _parse_rendered_sections(
    text: str,
    manifest_sections: Sequence[object],
) -> tuple[tuple[str, str], ...]:
    """Parse by manifest lengths so Markdown headings inside excerpts stay opaque."""
    sections: list[tuple[str, str]] = []
    cursor = 0
    for index, raw in enumerate(manifest_sections):
        if not isinstance(raw, Mapping):
            raise ValueError("historical prompt manifest contains an invalid section")
        heading = _manifest_string(raw, "heading")
        characters = raw.get("characters")
        if (
            isinstance(characters, bool)
            or not isinstance(characters, int)
            or characters < 0
        ):
            raise ValueError(
                "historical prompt manifest contains an invalid section character count"
            )
        prefix = f"## {heading}\n\n"
        if not text.startswith(prefix, cursor):
            raise ValueError("historical prompt section order does not match its manifest")
        cursor += len(prefix)
        end = cursor + characters
        if end > len(text):
            raise ValueError(
                "historical prompt section character count exceeds the packet"
            )
        sections.append((heading, text[cursor:end]))
        cursor = end
        if index < len(manifest_sections) - 1:
            if text[cursor:cursor + 2] != "\n\n":
                raise ValueError("historical prompt section separator is invalid")
            cursor += 2
    if cursor != len(text):
        raise ValueError("historical prompt has content outside its manifest sections")
    return tuple(sections)
