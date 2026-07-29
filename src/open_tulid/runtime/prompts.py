"""Deterministic model-facing prompt packets for frozen execution contracts."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from .execution_contracts import ExecutionContract

PROMPT_COMPILER_VERSION = 1
TOTAL_BUDGET = 6000
SECTION_BUDGETS = {
    "mission": 2500,
    "repository_facts": 300,
    "execution_contract": 800,
    "panalyzer_context": 2000,
    "completion_submission": 500,
}


@dataclass(frozen=True)
class PromptSection:
    id: str
    heading: str
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class PromptManifest:
    compiler_version: int
    execution_contract_sha256: str
    sections: tuple[Mapping[str, object], ...]
    packet_sha256: str
    characters: int

    def to_dict(self) -> dict[str, object]:
        return {
            "compiler_version": self.compiler_version,
            "execution_contract_sha256": self.execution_contract_sha256,
            "sections": [dict(section) for section in self.sections],
            "packet_sha256": self.packet_sha256,
            "characters": self.characters,
        }


@dataclass(frozen=True)
class CompiledPrompt:
    text: str
    sections: tuple[PromptSection, ...]
    manifest: PromptManifest


def compile_execution_prompt(contract: ExecutionContract) -> CompiledPrompt:
    """Compile only frozen contract content; never consult live tracker files."""
    generated = contract.generated_contract
    facts = contract.repository_facts
    checks = contract.resolved_checks
    sections = (
        _section("mission", "Mission", "\n".join((
            generated.objective,
            "\nImplement only this scoped task. Do not broaden the change surface.",
            "\nTask context:\n" + contract.source_task.body.strip(),
        ))),
        _section("repository_facts", "Repository Facts", "\n".join(filter(None, (
            "Repository is available." if facts.repository_available else "No repository is available.",
            "Top-level: " + ", ".join(facts.top_level_entries),
            "Manifests: " + ", ".join(facts.manifests),
            "Entrypoints: " + ", ".join(facts.detected_entrypoints),
        )))),
        _section("execution_contract", "Execution Contract", _contract_text(contract)),
        _section("panalyzer_context", "Panalyzer Context", ""),
        _section("selected_context_excerpts", "Selected Context Excerpts", _excerpts_text(contract)),
        _section("required_validation", "Required Validation", _checks_text(checks)),
        _section("execution_procedure", "Execution Procedure", "\n".join((
            "1. Inspect only the named seams before editing.",
            "2. Make the smallest coherent change inside the allowed surface.",
            "3. Run the required validation commands.",
            "4. If a failure is outside scope or pre-existing, report it instead of broadening the task.",
        ))),
        _section("completion_submission", "Completion Submission", _completion_text(contract)),
    )
    # Empty optional sections have a stable explanatory body rather than disappearing.
    sections = tuple(
        section if section.text else PromptSection(section.id, section.heading, "No context was selected.")
        for section in sections
    )
    sections = _fit_total_budget(sections)
    rendered = _render_sections(sections)
    packet_sha = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    manifest_sections = tuple({
        "id": section.id,
        "heading": section.heading,
        "sha256": hashlib.sha256(section.text.encode("utf-8")).hexdigest(),
        "characters": len(section.text),
        "truncated": section.truncated,
    } for section in sections)
    return CompiledPrompt(
        text=rendered,
        sections=sections,
        manifest=PromptManifest(PROMPT_COMPILER_VERSION, contract.sha256, manifest_sections, packet_sha, len(rendered)),
    )


def _section(id: str, heading: str, text: str) -> PromptSection:
    budget = SECTION_BUDGETS.get(id)
    clean = text.strip()
    if budget is not None and len(clean) > budget:
        return PromptSection(id, heading, clean[:budget].rstrip(), True)
    return PromptSection(id, heading, clean)


def _render_sections(sections: tuple[PromptSection, ...]) -> str:
    return "\n\n".join(f"## {section.heading}\n\n{section.text}" for section in sections)


def _fit_total_budget(sections: tuple[PromptSection, ...]) -> tuple[PromptSection, ...]:
    """Trim background first while preserving all required singleton sections."""
    mutable = list(sections)
    for section_id in ("panalyzer_context", "selected_context_excerpts", "mission", "execution_contract", "repository_facts", "required_validation", "execution_procedure"):
        overflow = len(_render_sections(tuple(mutable))) - TOTAL_BUDGET
        if overflow <= 0:
            break
        index = next((i for i, item in enumerate(mutable) if item.id == section_id), None)
        if index is None:
            continue
        item = mutable[index]
        # Keep a small explicit marker so a worker never mistakes a vanished
        # section for unselected or unavailable context.
        keep = max(40, len(item.text) - overflow)
        if keep < len(item.text):
            mutable[index] = PromptSection(item.id, item.heading, item.text[:keep].rstrip(), True)
    rendered = _render_sections(tuple(mutable))
    if len(rendered) > TOTAL_BUDGET:
        # This can only occur if headings/fixed completion text alone exceed the
        # packet cap; fail closed rather than silently losing the completion block.
        raise ValueError("fixed prompt sections exceed the total packet budget")
    return tuple(mutable)


def _contract_text(contract: ExecutionContract) -> str:
    generated = contract.generated_contract
    surface = generated.change_surface
    lines = ["Allowed paths: " + ", ".join((*surface.add, *surface.edit))]
    if surface.forbidden:
        lines.append("Forbidden paths: " + ", ".join(surface.forbidden))
    if generated.interfaces:
        lines.append("Interfaces:")
        lines.extend(f"- {item.symbol}: {item.behavior}" for item in generated.interfaces)
    if generated.requirements:
        lines.append("Requirements:")
        lines.extend(f"- {item}" for item in generated.requirements)
    if generated.non_goals:
        lines.append("Non-goals: " + "; ".join(generated.non_goals))
    return "\n".join(lines)


def _checks_text(checks) -> str:
    if not checks:
        return "No validation commands were resolved."
    return "\n".join(f"- {check.id}: `{' '.join(check.argv)}`" if check.argv else f"- {check.id}" for check in checks)


def _excerpts_text(contract: ExecutionContract) -> str:
    if not contract.context_excerpts:
        return "No additional context excerpts were selected for this job."
    return "\n\n".join(excerpt.text for excerpt in contract.context_excerpts)


def _completion_text(contract: ExecutionContract) -> str:
    validation_ids = _compact_list(check.id for check in contract.resolved_checks) or "none"
    artifacts = "[]" if not contract.transition.requires.artifacts else "[required artifacts]"
    return "\n".join((
        f"Required validations: {validation_ids}",
        "Submit completion exactly once after the work is complete.",
        "```sh",
        "curl -sS -X POST \\",
        "  -H \"content-type: application/json\" \\",
        "  -H \"x-open-tulid-completion-token: $OPEN_TULID_COMPLETION_TOKEN\" \\",
        "  \"$OPEN_TULID_COMPLETION_ENDPOINT\" \\",
        "  --data-binary @- <<'JSON'",
        "{\"summary\": \"what changed\", \"artifacts\": " + artifacts + ", \"changed_files\": [], \"validation_evidence\": {}}",
        "JSON",
        "```",
    ))


def _compact_list(values, *, limit: int = 120) -> str:
    text = ", ".join(values)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
