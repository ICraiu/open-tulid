from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    tracker_path: str
    repo_root: Path | None = None
    main_branch: str = "main"
    workflow_path: Path | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    docker_executable: str = "docker"
    shared_workspace_root: Path | None = None
    container_workspace: str = "/workspace/project"
    repo_execution_mode: str = "serial"
    image_tag_prefix: str = "open-tulid/agent"
    default_timeout_seconds: int = 7200
    failed_job_backoff_seconds: int = 60
    max_failed_attempts_per_transition: int = 0
    worker_images: dict[str, str] = field(default_factory=dict)
    worker_args: dict[str, tuple[str, ...]] = field(default_factory=dict)
    worker_resources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    worker_types: dict[str, str] = field(default_factory=dict)
    worker_model_env: dict[str, dict[str, str]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    completion_host: str = "0.0.0.0"
    completion_port: int = 0
    completion_container_host: str = "host.docker.internal"
    container_volume_relabel: bool = False


@dataclass(frozen=True)
class ResourceConfig:
    kind: str
    capacity: int
    proxy: str | None = None


@dataclass(frozen=True)
class ModelProxyConfig:
    kind: str
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_file: Path | None = None
    auth_home: Path | None = None
    container_auth_home: str | None = None


@dataclass(frozen=True)
class ModelProxyServerConfig:
    host: str = "0.0.0.0"
    port: int = 8787
    log_root: Path | None = None
    body_logging: str = "metadata"


@dataclass
class Config:
    vault_root: Path
    projects: list[str]
    tracker_type: str = ""
    config_dir: Path | None = None
    project_configs: dict[str, ProjectConfig] = field(default_factory=dict)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    resources: dict[str, ResourceConfig] = field(default_factory=dict)
    model_proxy: dict[str, ModelProxyConfig] = field(default_factory=dict)
    model_proxy_server: ModelProxyServerConfig = field(default_factory=ModelProxyServerConfig)

    @property
    def tracker_root(self) -> Path:
        return self.vault_root


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
