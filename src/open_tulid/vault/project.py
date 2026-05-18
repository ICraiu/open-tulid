from __future__ import annotations

import sys
from pathlib import Path

from open_tulid.models import Config, CreatedProject, Project, ProjectConfig
from open_tulid.config import CONFIG_FILENAME
from ruamel.yaml import YAML


REQUIRED_DIRS = ["kanban", "docs", "tasks", "events", "agents"]


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def create_project(config: Config, name: str) -> CreatedProject:
    if not name or not name.strip():
        _fail("Project name must not be empty")

    name = name.strip()
    # Validate name doesn't escape or contain path separators
    if "/" in name:
        _fail(f"Project name contains '/': {name}")
    if "\\" in name:
        _fail(f"Project name contains '\\': {name}")
    if ".." in name:
        _fail(f"Project name contains '..': {name}")
    if Path(name).is_absolute():
        _fail(f"Project name is an absolute path: {name}")

    project_config = config.project_configs.get(name)
    tracker_path = project_config.tracker_path if project_config is not None else name
    project_path = config.vault_root / tracker_path

    if project_path.exists():
        _fail(f"Project directory already exists: {project_path}")

    abs_vault = config.vault_root.resolve()
    abs_project = (abs_vault / tracker_path).resolve()
    if not str(abs_project).startswith(str(abs_vault) + "/") and abs_project != abs_vault:
        _fail(f"Project name would escape vault root: {name}")

    created_dirs: list[str] = []
    try:
        for dir_name in REQUIRED_DIRS:
            dir_path = project_path / dir_name
            dir_path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(f"{name}/{dir_name}")
        (project_path / "workflow.yaml").write_text("", encoding="utf-8")
        (project_path / "agents" / "default.agent.md").write_text(
            "# Default Agent Instructions\n\n"
            "Add project-wide coding standards and completion guidance here.\n",
            encoding="utf-8",
        )
    except OSError as e:
        _fail(f"Failed to create project directory: {e}")

    if project_config is None:
        _track_project(config, name)

    return CreatedProject(name=name, path=project_path, created_dirs=created_dirs)


def _track_project(config: Config, name: str) -> None:
    workflow_path = config.vault_root / name / "workflow.yaml"
    config.projects.append(name)
    config.project_configs[name] = ProjectConfig(
        name=name,
        tracker_path=name,
        workflow_path=workflow_path,
    )

    if config.config_dir is None:
        return

    config_path = config.config_dir / CONFIG_FILENAME
    yaml = YAML()
    data = yaml.load(config_path.read_text(encoding="utf-8")) or {}
    projects = data.setdefault("projects", {})
    projects[name] = {"path": name}
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


def iter_configured_projects(config: Config) -> list[Project]:
    projects: list[Project] = []

    for name in config.projects:
        project_config = config.project_configs.get(name)
        tracker_path = project_config.tracker_path if project_config is not None else name
        project_path = config.vault_root / tracker_path
        projects.append(Project(
            name=name,
            path=project_path,
            tracker_path=tracker_path,
            repo_root=project_config.repo_root if project_config is not None else None,
            main_branch=project_config.main_branch if project_config is not None else "main",
        ))

    return projects
