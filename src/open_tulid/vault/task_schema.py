from __future__ import annotations

from pathlib import Path
from typing import AbstractSet

from open_tulid.domain import DomainError
from open_tulid.runtime.acceptance_profiles import load_acceptance_profiles
from open_tulid.runtime.standard_contracts import load_standard_contract

# Required five-part shape for every task file. The `<description>` is the
# prose paragraph between the title and the first `## ` section.
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

# Machine-checkable acceptance block markers. `accepts:` may reference only
# declared project profiles/global command ids. `run:` is deliberately absent:
# per-task command lists are rejected.
MACHINE_KEYS = ("accepts:", "accepts_if:")


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
    """
    errors: list[DomainError] = []
    description_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("# "):
            # Leading title; following prose becomes the description.
            current = None
            continue
        if stripped.startswith("## "):
            name = stripped[3:].strip()
            sections[name] = []
            current = name
            continue
        (sections[current].append(stripped) if current is not None else description_lines.append(stripped))

    if not _has_content(description_lines):
        errors.append(_error(SECTION_MISSING, "Task is missing the required one-or-two-sentence description.", location))

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
