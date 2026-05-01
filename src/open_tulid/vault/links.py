from __future__ import annotations

import re
from pathlib import Path

from open_tulid.models import Project, ValidationError, ValidationReport

TASK_ROW_PATTERNS = [
    re.compile(r"^\s*-\s+\[\s\]\s+\[\[([^\]|#/\\]+)\]\]\s*$"),
    re.compile(r"^\s*-\s+\[\[([^\]|#/\\]+)\]\]\s*$"),
    re.compile(r"^\s*\[\[([^\]|#/\\]+)\]\]\s*$"),
]


def parse_task_row(line: str) -> str | None:
    for pattern in TASK_ROW_PATTERNS:
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return None


def resolve_task_link(project: Project, task_name: str) -> Path:
    return project.path / "tasks" / f"{task_name}.md"


def validate_kanban_file(project: Project, path: Path) -> ValidationReport:
    report = ValidationReport()

    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    in_frontmatter = False
    in_settings = False
    section_found = False
    frontmatter_start = 1

    while i < len(lines):
        line = lines[i]

        # Handle frontmatter
        if not in_frontmatter and not in_settings:
            if line.strip() == "---":
                in_frontmatter = True
                frontmatter_start = i + 1
                i += 1
                continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
                i += 1
                continue
            i += 1
            continue

        # Handle kanban settings block
        if not in_settings:
            if line.strip() == "%% kanban:settings":
                in_settings = True
                i += 1
                continue
        else:
            if line.strip() == "%%":
                in_settings = False
                i += 1
                continue
            i += 1
            continue

        # Process content lines
        stripped = line.strip()
        if stripped == "":
            i += 1
            continue

        if stripped.startswith("## "):
            section_found = True
            i += 1
            continue

        # Must be a task row
        task_name = parse_task_row(line)
        if task_name is None:
            report.errors.append(ValidationError(
                path=path,
                line=i + 1,
                message="Task row must contain exactly one task link like [[Task 1]].",
            ))
            i += 1
            continue

        if not section_found:
            report.errors.append(ValidationError(
                path=path,
                line=i + 1,
                message="Task row appears before any section heading.",
            ))

        report.checked_task_links += 1
        task_path = resolve_task_link(project, task_name)
        if not task_path.is_file():
            report.errors.append(ValidationError(
                path=path,
                line=i + 1,
                message=f"Linked task file does not exist: [[{task_name}]]",
            ))

        i += 1

    # Check for unclosed frontmatter
    if in_frontmatter:
        report.errors.append(ValidationError(
            path=path,
            line=frontmatter_start,
            message="Unclosed frontmatter (missing closing '---').",
        ))

    # Check for unclosed kanban settings
    if in_settings:
        # Find the line number of the opening
        for idx, l in enumerate(lines):
            if l.strip() == "%% kanban:settings":
                report.errors.append(ValidationError(
                    path=path,
                    line=idx + 1,
                    message="Unclosed kanban settings block (missing closing '%%').",
                ))
                break

    report.checked_kanban_files += 1
    return report
