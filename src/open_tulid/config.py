from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from open_tulid.models import Config, ProjectConfig, RuntimeConfig


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
    runtime = _load_runtime_config(data, config_dir)

    return Config(
        vault_root=vault_root,
        projects=validated_projects,
        config_dir=config_dir,
        workflow_path=workflow_path,
        project_configs=project_configs,
        runtime=runtime,
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


def _load_runtime_config(data: dict, config_dir: Path) -> RuntimeConfig:
    raw = data.get("runtime", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail("[runtime] must be a table when present")

    docker_executable = _runtime_string(
        raw, "docker_executable", default="docker", table="runtime",
    )
    container_workspace = _runtime_string(
        raw, "container_workspace", default="/workspace/project", table="runtime",
    )
    if not container_workspace.startswith("/"):
        _fail("runtime.container_workspace must be an absolute container path")

    image_tag_prefix = _runtime_string(
        raw, "image_tag_prefix", default="open-tulid/agent", table="runtime",
    )

    timeout = raw.get("default_timeout_seconds", 3600)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        _fail("runtime.default_timeout_seconds must be an integer")
    if timeout <= 0:
        _fail("runtime.default_timeout_seconds must be positive")

    shared_root = _runtime_optional_path(raw, "shared_workspace_root", config_dir, table="runtime")

    worker_images = _runtime_string_map(raw.get("worker_images", {}), "runtime.worker_images")
    worker_args = _runtime_string_sequence_map(raw.get("worker_args", {}), "runtime.worker_args")
    env = _runtime_string_map(raw.get("env", {}), "runtime.env")
    completion_host = _runtime_string(
        raw, "completion_host", default="0.0.0.0", table="runtime",
    )
    completion_port = raw.get("completion_port", 0)
    if isinstance(completion_port, bool) or not isinstance(completion_port, int):
        _fail("runtime.completion_port must be an integer")
    if completion_port < 0 or completion_port > 65535:
        _fail("runtime.completion_port must be between 0 and 65535")
    completion_container_host = _runtime_string(
        raw, "completion_container_host", default="host.docker.internal", table="runtime",
    )
    container_volume_relabel = raw.get("container_volume_relabel", False)
    if not isinstance(container_volume_relabel, bool):
        _fail("runtime.container_volume_relabel must be a boolean")

    return RuntimeConfig(
        docker_executable=docker_executable,
        shared_workspace_root=shared_root,
        container_workspace=container_workspace,
        image_tag_prefix=image_tag_prefix,
        default_timeout_seconds=timeout,
        worker_images=worker_images,
        worker_args=worker_args,
        env=env,
        completion_host=completion_host,
        completion_port=completion_port,
        completion_container_host=completion_container_host,
        container_volume_relabel=container_volume_relabel,
    )


def _runtime_string(raw: dict, key: str, *, default: str, table: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        _fail(f"{table}.{key} must be a string")
    if not value.strip():
        _fail(f"{table}.{key} must not be empty")
    return value.strip()


def _runtime_optional_path(raw: dict, key: str, config_dir: Path, *, table: str) -> Path | None:
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
    return path.resolve()


def _runtime_string_map(value: object, table: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(f"{table} must be a table")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            _fail(f"{table} keys must be strings")
        if not isinstance(item, str):
            _fail(f"{table}.{key} must be a string")
        if not key.strip() or not item.strip():
            _fail(f"{table} entries must not be empty")
        if table == "runtime.env" and _looks_secret_like(key):
            _fail(
                f"{table}.{key} looks secret-like. "
                "Do not pass secrets directly into worker containers."
            )
        result[key.strip()] = item.strip()
    return result


def _runtime_string_sequence_map(value: object, table: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(f"{table} must be a table")
    result: dict[str, tuple[str, ...]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            _fail(f"{table} keys must be non-empty strings")
        if not isinstance(item, list):
            _fail(f"{table}.{key} must be a list of strings")
        values: list[str] = []
        for index, entry in enumerate(item):
            if not isinstance(entry, str) or not entry.strip():
                _fail(f"{table}.{key}[{index}] must be a non-empty string")
            values.append(entry.strip())
        result[key.strip()] = tuple(values)
    return result


def _looks_secret_like(key: str) -> bool:
    normalized = key.upper()
    secret_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")
    return any(marker in normalized for marker in secret_markers)
