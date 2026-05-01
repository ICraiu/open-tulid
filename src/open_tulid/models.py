from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    vault_root: Path
    projects: list[str]


@dataclass
class Project:
    name: str
    path: Path


@dataclass
class ValidationError:
    path: Path | None
    line: int | None
    message: str


@dataclass
class ValidationReport:
    errors: list[ValidationError] = field(default_factory=list)
    checked_projects: int = 0
    checked_kanban_files: int = 0
    checked_task_links: int = 0

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


@dataclass
class CreatedProject:
    name: str
    path: Path
    created_dirs: list[str]
