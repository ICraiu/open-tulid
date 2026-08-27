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
from .verifier import VERIFICATION_REPORT_SCHEMA

TOTAL_BUDGET = 6000
SECTION_BUDGETS = {
    "mission": 2500,
    "repository_facts": 300,
    "selected_context_excerpts": 2400,
    "prior_implementation_evidence": 2000,
    "completion_submission": 900,
}
_OPTIONAL_TRIM_ORDER = (
    "mission",
    "repository_facts",
)
_UNRESOLVED_MARKER_RE = re.compile(r"\{\{[^{}]+\}\}|<TODO>|<TBD>|\bFIXME_PROMPT\b")
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

    def to_dict(self) -> dict[str, object]:
        return {
            "compiler_version": self.compiler_version,
            "packet_type": self.packet_type,
            "execution_contract_sha256": self.execution_contract_sha256,
            "sections": [dict(section) for section in self.sections],
            "packet_sha256": self.packet_sha256,
            "characters": self.characters,
            "character_budget": self.character_budget,
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
) -> ReviewEvidence | None:
    """Select the newest accepted job that produced the review transition's source state."""
    from_state = str(getattr(review_transition, "from_state", ""))
    candidates: list[ExecutionJob] = []
    for job in jobs:
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
            and report.get("schema") == VERIFICATION_REPORT_SCHEMA
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
            if validation.count(f"- {check.id}:") != 1 or (command and validation.count(command) != 1):
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
    )
    compiled = CompiledPrompt(text=text, sections=tuple(sections), manifest=manifest)
    issues = lint_compiled_prompt(compiled)
    fatal = tuple(issue for issue in issues if issue.code.endswith("hash_mismatch"))
    if fatal:
        raise ValueError(fatal[0].message)
    return compiled


def _compile_implementation_prompt(contract: ExecutionContract) -> CompiledPrompt:
    generated = contract.generated_contract
    facts = contract.repository_facts
    sections = (
        _section(
            "mission", "Mission",
            "\n".join((
                generated.objective,
                "\nImplement only this scoped task. Do not broaden the change surface.",
                "\nUser task rationale (cannot expand the contract):\n"
                + contract.source_task.body.strip(),
            )),
            "execution_contract", "source.task", "States the bounded implementation outcome.",
            truncatable=True,
        ),
        _section(
            "repository_facts", "Repository Facts",
            "\n".join(filter(None, (
                "Repository is available." if facts.repository_available else "No repository is available.",
                "Top-level: " + ", ".join(facts.top_level_entries),
                "Manifests: " + ", ".join(facts.manifests),
                "Entrypoints: " + ", ".join(facts.detected_entrypoints),
            ))),
            "repository_facts", "repository.facts", "Provides observed toolchain and entrypoint facts.",
            truncatable=True,
        ),
        _section(
            "execution_contract", "Execution Contract", _contract_text(contract),
            "execution_contract", "generated_contract", "Defines binding scope, behavior, and interfaces.",
        ),
        _section(
            "selected_context_excerpts", "Selected Context Excerpts", _excerpts_text(contract),
            "context_excerpt",
            (
                ", ".join(
                    f"{excerpt.artifact}#{excerpt.heading}"
                    for excerpt in contract.context_excerpts
                )
                or "none"
            ),
            (
                "; ".join(excerpt.reason for excerpt in contract.context_excerpts)
                or "No additional reference excerpt was required."
            ),
        ),
        _section(
            "required_validation", "Required Validation", _checks_text(contract.resolved_checks),
            "execution_contract", "resolved_checks", "Lists the exact checks Tulid will independently run.",
        ),
        _section(
            "execution_procedure", "Execution Procedure", "\n".join((
                "1. Read the mission, allowed scope, interfaces, and checks.",
                "2. Inspect only the named paths and integration seams before editing.",
                "3. Make the smallest coherent implementation.",
                "4. Run the narrowest focused check, then every required project check.",
                "5. Compare actual changes with the allowed surface.",
                "6. Submit evidence, or stop on an out-of-scope, baseline, or environment blocker.",
            )),
            "runtime_policy", f"compiler/v{PROMPT_COMPILER_VERSION}",
            "Provides one implementation-model inspect-implement-test-audit-submit loop.",
        ),
        _section(
            "completion_submission", "Completion Submission", _completion_text(contract),
            "runtime", "completion_api", "Provides the sole completion mechanism.",
        ),
    )
    return _finalize(contract, "implementation", sections)


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
                f"Audit the implementation produced for: {contract.generated_contract.objective}",
                "Use the authoritative prior implementation evidence below.",
                "Find a concrete in-scope defect and correct it, or submit a valid no-change review.",
                "Do not restart implementation or perform unrelated cleanup.",
            )),
            "execution_contract", "generated_contract.objective",
            "Frames self-review as an evidence-backed audit rather than another implementation pass.",
            truncatable=True,
        ),
        _section(
            "prior_implementation_evidence", "Prior Implementation Evidence",
            _review_evidence_text(prior),
            "historical_job", evidence.source_job_id,
            "Supplies the authoritative prior diff summary, checks, and repair history.",
        ),
        _section(
            "execution_contract", "Review Contract",
            _contract_text(contract, include_requirements=False),
            "execution_contract", "generated_contract",
            "Keeps review corrections inside the original behavior and scope.",
        ),
        _section(
            "requirements_audit", "Requirements Audit",
            "\n".join(f"- {item}" for item in contract.generated_contract.requirements),
            "execution_contract", "generated_contract.requirements",
            "Lists behavior that review must map to code or test evidence.",
        ),
        _section(
            "required_validation", "Required Validation", _checks_text(contract.resolved_checks),
            "execution_contract", "resolved_checks",
            "Lists fresh checks required after review, including a no-change review.",
        ),
        _section(
            "review_procedure", "Review Procedure", "\n".join((
                "1. Map every requirement to prior check, code, or test evidence.",
                "2. Inspect the authoritative changed files and named interfaces.",
                "3. Identify a concrete in-scope defect, if any.",
                "4. Make only a targeted correction; otherwise leave the workspace unchanged.",
                "5. Run affected focused checks and every required project check.",
                "6. Submit the correction or an explicit no-change review with fresh evidence.",
            )),
            "runtime_policy", f"compiler/v{PROMPT_COMPILER_VERSION}",
            "Provides the distinct contract-to-change review loop.",
        ),
        _section(
            "scope_reminder", "Review Boundaries",
            "Do not add abstractions, broaden tests, create planning reports, or edit unrelated files. "
            "An empty diff is correct when no concrete defect exists.",
            "runtime_policy", f"compiler/v{PROMPT_COMPILER_VERSION}",
            "Prevents gratuitous review edits and broad cleanup.",
        ),
        _section(
            "completion_submission", "Completion Submission", _completion_text(contract),
            "runtime", "completion_api", "Provides the sole completion mechanism.",
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
    sections = _fit_total_budget(sections)
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
        ),
    )
    lint = lint_compiled_prompt(compiled, contract=contract)
    if lint:
        raise ValueError(f"{lint[0].code}: {lint[0].message}")
    return compiled


def _render_sections(sections: tuple[PromptSection, ...]) -> str:
    return "\n\n".join(f"## {section.heading}\n\n{section.text}" for section in sections)


def _fit_total_budget(sections: tuple[PromptSection, ...]) -> tuple[PromptSection, ...]:
    """Trim only background sections; binding scope, checks, and completion never truncate."""
    mutable = list(sections)
    for section_id in _OPTIONAL_TRIM_ORDER:
        overflow = len(_render_sections(tuple(mutable))) - TOTAL_BUDGET
        if overflow <= 0:
            break
        index = next((i for i, item in enumerate(mutable) if item.id == section_id), None)
        if index is None:
            continue
        item = mutable[index]
        keep = max(80, len(item.text) - overflow)
        if keep < len(item.text):
            mutable[index] = PromptSection(
                item.id,
                item.heading,
                _truncate(item.text, keep),
                item.source_kind,
                item.source_ref,
                item.selection_reason,
                item.budget,
                True,
            )
    if len(_render_sections(tuple(mutable))) > TOTAL_BUDGET:
        raise ValueError(
            "binding prompt sections exceed the total packet budget; split or refine the contract"
        )
    return tuple(mutable)


def _contract_text(
    contract: ExecutionContract,
    *,
    include_requirements: bool = True,
) -> str:
    generated = contract.generated_contract
    surface = generated.change_surface
    lines = [f"Objective: {generated.objective}"]
    lines.append("Allowed additions: " + (", ".join(surface.add) or "none"))
    lines.append("Allowed edits: " + (", ".join(surface.edit) or "none"))
    if surface.forbidden:
        lines.append("Forbidden paths: " + ", ".join(surface.forbidden))
    if generated.interfaces:
        lines.append("Interfaces:")
        lines.extend(
            f"- {item.symbol}"
            + (f" {item.signature}" if item.signature else "")
            + f": {item.behavior}"
            for item in generated.interfaces
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
    if generated.vertical_slice_exemption is not None:
        lines.append(
            "Vertical-slice exemption: " + generated.vertical_slice_exemption
        )
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


def _completion_text(contract: ExecutionContract) -> str:
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
    """Return whether a transition requires prior implementation evidence."""
    value = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        " ",
        f"{getattr(transition, 'id', '')} "
        f"{getattr(transition, 'from_state', '')}",
    )
    tokens = tuple(
        token
        for token in re.split(r"[^a-z0-9]+", value.casefold())
        if token
    )
    return "review" in tokens


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
