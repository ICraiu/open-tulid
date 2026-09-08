from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet

from open_tulid.domain import DomainError

# Required five-part shape for every implementation task file. The
# `<description>` is the prose paragraph between the title and the first
# `## ` section.
REQUIRED_SECTIONS = ("Why", "What", "How", "Acceptance")

# Diagnostics are presence-only: a schema can enforce shape and presence, never
# quality of fill. How well a section is filled is a prompt-side matter for the
# authoring agent.
SECTION_MISSING = "task.section_missing"
SECTION_EMPTY = "task.section_empty"
ACCEPTANCE_CRITERIA_MISSING = "task.acceptance_criteria_missing"
ACCEPTANCE_RUN_UNKNOWN = "task.acceptance_run_unknown"
# Per-task verification command lists are forbidden: verification commands are
# global at the project level (contract.yaml) and are inherited by every
# implementation task. A task may add tests to the project suite, but it must
# not declare its own `run:` command list.
ACCEPTANCE_RUN_FORBIDDEN = "task.acceptance_run_forbidden"

# A task body must carry a Markdown H1 title and unique `## ` section headings.
TITLE_MISSING = "task.title_missing"
DUPLICATE_HEADING = "task.duplicate_heading"

# Machine-checkable acceptance block markers. `accepts:` may reference only
# declared project profiles/global command ids. `run:` is deliberately absent:
# per-task command lists are rejected.
MACHINE_KEYS = ("accepts:", "accepts_if:")


@dataclass(frozen=True)
class TaskSection:
    """One `## `-level section extracted from a task body."""

    name: str
    content: tuple[str, ...]


@dataclass(frozen=True)
class ParsedTaskBody:
    """Structural view of one task body shared by every parsing consumer.

    Presence-only: the parser describes where prose and sections live; it does
    not claim that a requirement is well specified. Behavior/coverage
    assessment is deliberately left to planning and review instructions.
    """

    title: str | None
    description_lines: tuple[str, ...]
    sections: tuple[TaskSection, ...]

    @property
    def duplicate_heading_names(self) -> tuple[str, ...]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for section in self.sections:
            if section.name in seen:
                duplicates.append(section.name)
            seen.add(section.name)
        return tuple(duplicates)

    def content_by_heading(self) -> dict[str, list[str]]:
        """Collapse section content by heading name, preserving heading order.

        Duplicate headings are merged so required-section presence checks can
        inspect every line; duplicate detection is reported separately by
        :meth:`duplicate_heading_names`.
        """
        collapsed: dict[str, list[str]] = {}
        for section in self.sections:
            collapsed.setdefault(section.name, []).extend(section.content)
        return collapsed


def parse_task_body(body: str) -> ParsedTaskBody:
    """Parse the full Markdown task body into its structural parts.

    The leading ``# `` line is the title; prose before the first ``## `` is the
    description. Every ``## `` line opens a section; later lines accumulate into
    it. Duplicate section headings are preserved (not coalesced) so callers can
    reject them precisely.
    """
    title: str | None = None
    description_lines: list[str] = []
    section_names: list[str] = []
    section_content: list[list[str]] = []
    current_index: int | None = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("# "):
            if title is None:
                title = stripped[2:].strip()
            current_index = None
            continue
        if stripped.startswith("## "):
            section_names.append(stripped[3:].strip())
            section_content.append([])
            current_index = len(section_names) - 1
            continue
        if not stripped:
            continue
        if current_index is not None:
            section_content[current_index].append(stripped)
        else:
            description_lines.append(stripped)
    return ParsedTaskBody(
        title=title,
        description_lines=tuple(description_lines),
        sections=tuple(
            TaskSection(name=name, content=tuple(lines))
            for name, lines in zip(section_names, section_content)
        ),
    )


def validate_task_structure(
    body: str,
    *,
    location: str | None = None,
    require_title: bool = True,
) -> tuple[DomainError, ...]:
    """Validate the shared structural shape of a task body.

    This is the single path used by derived-artifact acceptance, tracker
    loading/validation, and job preparation. It checks title presence and
    duplicate ``## `` headings. Callers that carry the title as a separate
    field (the domain ``Task.title``) may set ``require_title=False`` and keep
    only the duplicate-heading check.
    """
    parsed = parse_task_body(body)
    errors: list[DomainError] = []
    if require_title and not parsed.title:
        errors.append(_error(
            TITLE_MISSING,
            "Task body is missing a Markdown H1 title (e.g. `# Concrete outcome`).",
            location,
        ))
    for name in parsed.duplicate_heading_names:
        errors.append(_error(
            DUPLICATE_HEADING,
            f"Duplicate task section heading `## {name}`; section headings must appear exactly once.",
            location,
        ))
    return tuple(errors)


def validate_task_schema(
    body: str,
    declared_ids: AbstractSet[str],
    *,
    location: str | None = None,
) -> tuple[DomainError, ...]:
    """Validate the presence-only shape of one task file body.

    ``body`` is the full Markdown task body (including the leading ``# Title``).
    ``declared_ids`` is the set of project-declared profile ids and check ids
    that a machine `accepts:` block may reference. Unresolved references are
    reported as ``task.acceptance_run_unknown``.

    Tracker tasks may predate the five-part body shape and carry their title in
    a separate field, so the title is not required here; duplicate section
    headings always are.
    """
    parsed = parse_task_body(body)
    errors = list(validate_task_structure(body, location=location, require_title=False))
    if not _has_content(parsed.description_lines):
        errors.append(_error(SECTION_MISSING, "Task is missing the required one-or-two-sentence description.", location))

    sections = parsed.content_by_heading()

    for name in REQUIRED_SECTIONS:
        if name not in sections:
            errors.append(_error(
                SECTION_MISSING,
                f"Task is missing the required ## {name} section.",
                location,
            ))
            continue
        content = sections[name]
        if name == "Acceptance":
            errors.extend(_validate_acceptance(content, declared_ids, location))
        elif not _has_content(content):
            errors.append(_error(
                SECTION_EMPTY,
                f"The ## {name} section is present but empty.",
                location,
            ))
    return tuple(errors)


def _validate_acceptance(
    content: list[str],
    declared_ids: AbstractSet[str],
    location: str | None,
) -> list[DomainError]:
    errors: list[DomainError] = []
    has_accepts = False
    accepts: list[str] = []
    machine_key_seen = False
    free_prose = False
    for line in content:
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            continue
        if stripped.startswith("accepts:"):
            machine_key_seen = True
            has_accepts = True
            accepts = _parse_list(stripped)
        elif stripped.startswith("accepts_if"):
            machine_key_seen = True
        elif stripped.startswith("run:"):
            # A per-task command list is forbidden. Verification commands are
            # global (contract.yaml) and inherited by every task.
            machine_key_seen = True
            errors.append(_error(
                ACCEPTANCE_RUN_FORBIDDEN,
                "Per-task `run:` command lists are not allowed; verification "
                "commands are global at the project level and apply to every "
                "implementation task.",
                location,
            ))
        elif not _is_machine_line(stripped):
            free_prose = True

    if not machine_key_seen and not free_prose:
        errors.append(_error(
            ACCEPTANCE_CRITERIA_MISSING,
            "The ## Acceptance section has no per-task acceptance criteria "
            "(no machine-checkable block and no free-prose list).",
            location,
        ))
    if machine_key_seen:
        if not has_accepts:
            errors.append(_error(
                ACCEPTANCE_RUN_UNKNOWN,
                "The machine acceptance block must declare accepts: [<declared "
                "profile id or check id>].",
                location,
            ))
        for identifier in accepts:
            if identifier not in declared_ids:
                errors.append(_error(
                    ACCEPTANCE_RUN_UNKNOWN,
                    f"The machine block references undeclared check or profile "
                    f"id {identifier!r}.",
                    location,
                ))
    return errors


def _parse_list(line: str) -> list[str]:
    """Parse ``key: [item, item]`` into a list of trimmed string items."""
    _, _, value = line.partition(":")
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [part.strip() for part in value.split(",") if part.strip()]


def _is_machine_line(line: str) -> bool:
    return line.startswith(("accepts:", "accepts_if:"))


def _has_content(lines: list[str]) -> bool:
    return any(line.strip() for line in lines)


def declared_ids_for_project(project_root: Path) -> frozenset[str]:
    """Collect the declared profile ids and check ids for a project.

    `accepts:` references must resolve against these. Profile ids come from
    ``acceptance.yaml``; check ids from the declared project contract commands.
    """
    from open_tulid.runtime.acceptance_profiles import load_acceptance_profiles
    from open_tulid.runtime.standard_contracts import load_standard_contract

    declared: set[str] = set()
    profile_result = load_acceptance_profiles(project_root)
    if profile_result.accepted and profile_result.profiles is not None:
        declared.update(profile_result.profiles)
    contract_result = load_standard_contract(project_root)
    if contract_result.accepted and contract_result.contract is not None:
        declared.update(command.name for command in contract_result.contract.commands)
    return frozenset(declared)


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
