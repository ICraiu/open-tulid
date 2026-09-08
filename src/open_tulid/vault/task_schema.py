from __future__ import annotations

from dataclasses import dataclass

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
# Per-task acceptance command selectors are forbidden: verification commands are
# global at the project level (contract.yaml) and are inherited by every
# implementation task. A task may add tests to the project suite, but it must
# not declare its own `accepts:`, `accepts_if:`, or `run:` command selection.
ACCEPTANCE_RUN_FORBIDDEN = "task.acceptance_run_forbidden"

# A task body must carry a Markdown H1 title and unique `## ` section headings.
TITLE_MISSING = "task.title_missing"
DUPLICATE_HEADING = "task.duplicate_heading"


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
    *,
    location: str | None = None,
) -> tuple[DomainError, ...]:
    """Validate the presence-only shape of one task file body.

    ``body`` is the full Markdown task body (including the leading ``# Title``).

    Every task follows the five-part implementation shape: a description, then
    ``## Why``, ``## What``, ``## How``, and ``## Acceptance``. ``## Acceptance``
    is prose-only: per-task command selectors (``accepts:``, ``accepts_if:``,
    ``run:``) are forbidden. Verification commands are global at the project
    level (contract.yaml) and inherited by every task.

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
            errors.extend(_validate_acceptance(content, location))
        elif not _has_content(content):
            errors.append(_error(
                SECTION_EMPTY,
                f"The ## {name} section is present but empty.",
                location,
            ))
    return tuple(errors)


def _validate_acceptance(
    content: list[str],
    location: str | None,
) -> list[DomainError]:
    errors: list[DomainError] = []
    selector_seen = False
    free_prose = False
    for line in content:
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            continue
        if stripped.startswith(("accepts:", "accepts_if:")) or stripped.startswith("run:"):
            # Per-task command selectors are forbidden: verification commands
            # are global (contract.yaml) and inherited by every task.
            selector_seen = True
            errors.append(_error(
                ACCEPTANCE_RUN_FORBIDDEN,
                "Per-task acceptance command selectors (`accepts:`, "
                "`accepts_if:`, `run:`) are not allowed; verification commands "
                "are global at the project level and apply to every "
                "implementation task.",
                location,
            ))
        else:
            free_prose = True

    if not selector_seen and not free_prose:
        errors.append(_error(
            ACCEPTANCE_CRITERIA_MISSING,
            "The ## Acceptance section has no per-task acceptance criteria "
            "as prose.",
            location,
        ))
    return errors


def _has_content(lines: list[str]) -> bool:
    return any(line.strip() for line in lines)


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
