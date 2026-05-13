from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from open_tulid.models import Config, ProjectConfig


CONFIG_DIRNAME = ".tuluid"
CONFIG_FILENAME = "open-tulid.toml"
LEGACY_CONFIG_FILENAME = ".open-tulid.toml"


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def load_config(path: Path | None = None) -> Config:
    if path is None:
        cwd_config = Path.cwd() / CONFIG_DIRNAME / CONFIG_FILENAME
        if cwd_config.is_file():
            path = cwd_config
        else:
            home_config = Path.home() / CONFIG_DIRNAME / CONFIG_FILENAME
            if home_config.is_file():
                path = home_config
            else:
                legacy_cwd_config = Path.cwd() / LEGACY_CONFIG_FILENAME
                if legacy_cwd_config.is_file():
                    path = legacy_cwd_config
                else:
                    legacy_home_config = Path.home() / LEGACY_CONFIG_FILENAME
                    path = legacy_home_config if legacy_home_config.is_file() else home_config

    if not path.is_file():
        _fail(f"Config file not found: {path}")

    config_dir = path.parent
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))

    vault = data.get("vault")
    if vault is None:
        _fail("[vault] section is missing from config")
    if not isinstance(vault, dict):
        _fail("[vault] must be a table")

    vault_root_str = vault.get("root")
    if vault_root_str is None:
        _fail("vault.root is missing from config")
    if not isinstance(vault_root_str, str):
        _fail("vault.root must be a string")

    vault_root = Path(vault_root_str)
    if not vault_root.is_dir():
        _fail(f"vault.root does not point to an existing directory: {vault_root}")

    project_names = vault.get("projects")
    if project_names is None:
        _fail("vault.projects is missing from config")

    if not isinstance(project_names, list):
        _fail("vault.projects must be a list")

    if len(project_names) == 0:
        _fail("vault.projects must not be empty")

    validated_projects: list[str] = []
    for name in project_names:
        if not isinstance(name, str):
            _fail(f"Project name must be a string, got: {type(name).__name__}")
        _validate_project_name(name)
        validated_projects.append(name)

    project_configs = _load_project_configs(data, vault_root, config_dir, validated_projects)

    workflow_path = _load_workflow_path(data, config_dir)

    return Config(
        vault_root=vault_root,
        projects=validated_projects,
        config_dir=config_dir,
        workflow_path=workflow_path,
        project_configs=project_configs,
    )


def _validate_project_name(name: str) -> None:
    if "/" in name:
        _fail(f"Project name contains '/': {name}")
    if "\\" in name:
        _fail(f"Project name contains '\\': {name}")
    if ".." in name:
        _fail(f"Project name contains '..': {name}")
    if Path(name).is_absolute():
        _fail(f"Project name is an absolute path: {name}")


def _load_project_configs(
    data: dict,
    vault_root: Path,
    config_dir: Path,
    project_names: list[str],
) -> dict[str, ProjectConfig]:
    raw_projects = data.get("projects", {})
    if raw_projects is None:
        raw_projects = {}
    if not isinstance(raw_projects, dict):
        _fail("[projects] must be a table when present")

    unknown = sorted(set(raw_projects) - set(project_names))
    if unknown:
        _fail(f"[projects] contains entries not listed in vault.projects: {', '.join(unknown)}")

    configs: dict[str, ProjectConfig] = {}
    abs_vault = vault_root.resolve()
    for name in project_names:
        raw = raw_projects.get(name, {})
        if not isinstance(raw, dict):
            _fail(f"[projects.{name}] must be a table")
        tracker_path = _project_string(raw, "tracker_path", default=name, table=f"projects.{name}")
        _validate_tracker_path(tracker_path)
        candidate = (abs_vault / tracker_path).resolve()
        if not str(candidate).startswith(str(abs_vault) + os.sep) and candidate != abs_vault:
            _fail(f"Project tracker_path escapes vault root: {tracker_path}")

        repo_root = _project_optional_path(raw, "repo_root", config_dir, table=f"projects.{name}")
        main_branch = _project_string(raw, "main_branch", default="main", table=f"projects.{name}")
        if not main_branch.strip():
            _fail(f"projects.{name}.main_branch must not be empty")

        configs[name] = ProjectConfig(
            name=name,
            tracker_path=tracker_path,
            repo_root=repo_root,
            main_branch=main_branch,
        )
    return configs


def _project_string(raw: dict, key: str, *, default: str, table: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        _fail(f"{table}.{key} must be a string")
    if not value.strip():
        _fail(f"{table}.{key} must not be empty")
    return value.strip()


def _project_optional_path(raw: dict, key: str, config_dir: Path, *, table: str) -> Path | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{table}.{key} must be a string")
    if not value.strip():
        _fail(f"{table}.{key} must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    if not path.is_dir():
        _fail(f"{table}.{key} does not point to an existing directory: {path}")
    return path


def _validate_tracker_path(path: str) -> None:
    path_obj = Path(path)
    if path_obj.is_absolute():
        _fail(f"Project tracker_path is an absolute path: {path}")
    if any(part in {"", ".", ".."} for part in path_obj.parts):
        _fail(f"Project tracker_path contains unsafe path segments: {path}")
    if "\\" in path:
        _fail(f"Project tracker_path contains '\\': {path}")


def _load_workflow_path(data: dict, config_dir: Path) -> Path | None:
    workflow = data.get("workflow")
    if workflow is None:
        return None
    if not isinstance(workflow, dict):
        _fail("[workflow] must be a table")
    raw_path = workflow.get("path")
    if raw_path is None:
        return None
    if not isinstance(raw_path, str):
        _fail("workflow.path must be a string")
    workflow_path = Path(raw_path)
    if not workflow_path.is_absolute():
        workflow_path = config_dir / workflow_path
    return workflow_path
