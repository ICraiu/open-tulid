from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from open_tulid.domain.schema import DomainError, ProjectSnapshot, Task, WorkflowDefinition
from open_tulid.models import Project, ValidationReport


class AdapterCapability(str, Enum):
    LOAD_PROJECT = "load_project"
    READ_TASK = "read_task"
    WRITE_TASK = "write_task"
    CREATE_TASK = "create_task"
    MOVE_TASK = "move_task"
    APPEND_EVENT = "append_event"


@dataclass(frozen=True)
class AdapterResult:
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class LoadProjectResult(AdapterResult):
    snapshot: ProjectSnapshot | None = None


@dataclass(frozen=True)
class ReadTaskResult(AdapterResult):
    task: Task | None = None


@dataclass(frozen=True)
class WriteResult(AdapterResult):
    path: str | None = None


@dataclass(frozen=True)
class AdapterBuildRequest:
    project_id: str
    project_root: Path
    tracker_type: str
    workflow: WorkflowDefinition


@runtime_checkable
class StorageAdapter(Protocol):
    name: str
    capabilities: frozenset[AdapterCapability]

    def load_project(self) -> LoadProjectResult:
        """Load external storage into a pure domain project snapshot."""

    def read_task(self, task_id: str) -> ReadTaskResult:
        """Read one task by stable domain task ID."""

    def write_task(self, task: Task) -> WriteResult:
        """Persist a domain task without deciding workflow transitions."""

    def create_task(self, task: Task) -> WriteResult:
        """Persist a new task and place it into its initial tracker state."""

    def move_task(self, task_id: str, state: str) -> WriteResult:
        """Apply an already-approved logical task move."""

    def append_event(self, event: Mapping[str, Any]) -> WriteResult:
        """Append one structured event record."""


@runtime_checkable
class TrackerFormat(Protocol):
    name: str

    def parse_task_row(self, line: str) -> str | None:
        """Parse one tracker-specific board row into a task reference."""

    def validate_link_file(self, project: Project, path: Path) -> ValidationReport:
        """Validate one tracker-specific link/board file."""


@runtime_checkable
class StorageAdapterFactory(Protocol):
    adapter_type: str

    def build(self, request: AdapterBuildRequest) -> StorageAdapter:
        """Build one storage adapter instance from runtime configuration."""

    def build_tracker_format(self) -> TrackerFormat:
        """Build the tracker-specific syntax/validation implementation."""
