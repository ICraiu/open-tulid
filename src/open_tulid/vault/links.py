from __future__ import annotations

from pathlib import Path

from open_tulid.adapters.base import TrackerFormat
from open_tulid.models import Project, ValidationReport


def parse_task_row(line: str, tracker_format: TrackerFormat) -> str | None:
    return tracker_format.parse_task_row(line)


def validate_kanban_file(project: Project, path: Path, tracker_format: TrackerFormat) -> ValidationReport:
    return tracker_format.validate_link_file(project, path)
