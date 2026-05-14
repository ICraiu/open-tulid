from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    tracker_path: str
    repo_root: Path | None = None
    main_branch: str = "main"


@dataclass(frozen=True)
class RuntimeConfig:
    docker_executable: str = "docker"
    shared_workspace_root: Path | None = None
    container_workspace: str = "/workspace/project"
    image_tag_prefix: str = "open-tulid/agent"
    default_timeout_seconds: int = 3600
    worker_images: dict[str, str] = field(default_factory=dict)
    worker_args: dict[str, tuple[str, ...]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    completion_host: str = "0.0.0.0"
    completion_port: int = 0
    completion_container_host: str = "host.docker.internal"
    container_volume_relabel: bool = False


@dataclass
class Config:
    vault_root: Path
    projects: list[str]
    config_dir: Path | None = None
    workflow_path: Path | None = None
    project_configs: dict[str, ProjectConfig] = field(default_factory=dict)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


@dataclass
class Project:
    name: str
    path: Path
    tracker_path: str | None = None
    repo_root: Path | None = None
    main_branch: str = "main"


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
