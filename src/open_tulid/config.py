from __future__ import annotations

import os
import sys
from pathlib import Path

from ruamel.yaml import YAML

from open_tulid.adapters import supported_adapter_types
from open_tulid.models import (
    Config,
    ModelProxyConfig,
    ModelProxyServerConfig,
    ProjectConfig,
    ResourceConfig,
    RuntimeConfig,
)


CONFIG_DIRNAME = ".tulid"
CONFIG_FILENAME = "config.yaml"


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = Path.home() / CONFIG_DIRNAME / CONFIG_FILENAME

    if not path.is_file():
        _fail(f"Config file not found: {path}")

    config_dir = path.parent
    yaml = YAML(typ="safe")
    data = yaml.load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        _fail("config must be a YAML mapping")

    tracker = data.get("tracker")
    if not isinstance(tracker, dict):
        _fail("tracker section is missing from config")
    tracker_type = _required_table_string(tracker, "type", "tracker")
    supported_types = supported_adapter_types()
    if tracker_type not in supported_types:
        supported = ", ".join(supported_types)
        _fail(f"tracker.type must be one of: {supported}")
    tracker_root_str = _required_table_string(tracker, "root", "tracker")
    vault_root = Path(tracker_root_str).expanduser()
    if not vault_root.is_dir():
        _fail(f"tracker.root does not point to an existing directory: {vault_root}")

    project_configs = _load_project_configs(data, vault_root, config_dir)
    validated_projects = list(project_configs)
    runtime = _load_runtime_config(data, config_dir)
    resources = _load_resources(data)
    model_proxy = _load_model_proxy(data, config_dir)
    model_proxy_server = _load_model_proxy_server(data, config_dir)
    _validate_resource_proxy_refs(resources, model_proxy)
    _validate_worker_resource_compatibility(runtime, resources, model_proxy)

    return Config(
        vault_root=vault_root,
        projects=validated_projects,
        tracker_type=tracker_type,
        config_dir=config_dir,
        project_configs=project_configs,
        runtime=runtime,
        resources=resources,
        model_proxy=model_proxy,
        model_proxy_server=model_proxy_server,
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
) -> dict[str, ProjectConfig]:
    raw_projects = data.get("projects")
    if not isinstance(raw_projects, dict):
        _fail("projects must be a mapping")

    configs: dict[str, ProjectConfig] = {}
    abs_vault = vault_root.resolve()
    for name, raw in raw_projects.items():
        if not isinstance(name, str):
            _fail(f"Project name must be a string, got: {type(name).__name__}")
        _validate_project_name(name)
        if not isinstance(raw, dict):
            _fail(f"[projects.{name}] must be a table")
        tracker_path = _project_string(raw, "path", default=name, table=f"projects.{name}")
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
            workflow_path=candidate / "workflow.yaml",
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

    repo_execution_mode = _runtime_string(
        raw, "repo_execution_mode", default="serial", table="runtime",
    )
    if repo_execution_mode not in {"serial", "parallel"}:
        _fail("runtime.repo_execution_mode must be either 'serial' or 'parallel'")

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
    worker_resources = _runtime_string_sequence_map(
        raw.get("worker_resources", {}), "runtime.worker_resources",
    )
    worker_types = _runtime_string_map(raw.get("worker_types", {}), "runtime.worker_types")
    worker_model_env = _runtime_string_map_map(
        raw.get("worker_model_env", {}), "runtime.worker_model_env",
    )
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
        repo_execution_mode=repo_execution_mode,
        image_tag_prefix=image_tag_prefix,
        default_timeout_seconds=timeout,
        worker_images=worker_images,
        worker_args=worker_args,
        worker_resources=worker_resources,
        worker_types=worker_types,
        worker_model_env=worker_model_env,
        env=env,
        completion_host=completion_host,
        completion_port=completion_port,
        completion_container_host=completion_container_host,
        container_volume_relabel=container_volume_relabel,
    )


def _load_resources(data: dict) -> dict[str, ResourceConfig]:
    raw = data.get("resources", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail("[resources] must be a table when present")
    resources: dict[str, ResourceConfig] = {}
    for name, item in raw.items():
        if not isinstance(name, str) or not name.strip():
            _fail("resources keys must be non-empty strings")
        if not isinstance(item, dict):
            _fail(f"resources.{name} must be a table")
        kind = _required_table_string(item, "kind", f"resources.{name}")
        capacity = item.get("capacity")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            _fail(f"resources.{name}.capacity must be a positive integer")
        proxy = item.get("proxy")
        if proxy is not None and (not isinstance(proxy, str) or not proxy.strip()):
            _fail(f"resources.{name}.proxy must be a non-empty string")
        resources[name.strip()] = ResourceConfig(kind=kind, capacity=capacity, proxy=proxy.strip() if proxy else None)
    return resources


def _load_model_proxy(data: dict, config_dir: Path) -> dict[str, ModelProxyConfig]:
    raw = data.get("model_proxy", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail("[model_proxy] must be a table when present")
    proxies: dict[str, ModelProxyConfig] = {}
    for name, item in raw.items():
        if not isinstance(name, str) or not name.strip():
            _fail("model_proxy keys must be non-empty strings")
        if not isinstance(item, dict):
            _fail(f"model_proxy.{name} must be a table")
        kind = _required_table_string(item, "kind", f"model_proxy.{name}")
        if kind not in {"local", "openai", "subscription"}:
            _fail(f"model_proxy.{name}.kind must be local, openai, or subscription")
        base_url = None
        if kind in {"local", "openai"}:
            base_url = _required_table_string(item, "base_url", f"model_proxy.{name}")
        api_key_env = item.get("api_key_env")
        if api_key_env is not None and (not isinstance(api_key_env, str) or not api_key_env.strip()):
            _fail(f"model_proxy.{name}.api_key_env must be a non-empty string")
        api_key_file = item.get("api_key_file")
        if api_key_file is not None:
            if not isinstance(api_key_file, str) or not api_key_file.strip():
                _fail(f"model_proxy.{name}.api_key_file must be a non-empty string")
            api_key_file = _resolve_config_path(api_key_file, config_dir)
        auth_home = item.get("auth_home")
        container_auth_home = item.get("container_auth_home")
        if kind == "openai" and (api_key_env is None) == (api_key_file is None):
            _fail(f"model_proxy.{name} must configure exactly one of api_key_env or api_key_file")
        if kind == "subscription":
            if not isinstance(auth_home, str) or not auth_home.strip():
                _fail(f"model_proxy.{name}.auth_home is required for subscription proxies")
            auth_home = _resolve_config_path(auth_home, config_dir)
            if container_auth_home is None:
                container_auth_home = "/root/.codex"
            if not isinstance(container_auth_home, str) or not container_auth_home.startswith("/"):
                _fail(f"model_proxy.{name}.container_auth_home must be an absolute container path")
        proxies[name.strip()] = ModelProxyConfig(
            kind=kind,
            base_url=base_url,
            api_key_env=api_key_env.strip() if isinstance(api_key_env, str) else None,
            api_key_file=api_key_file,
            auth_home=auth_home if isinstance(auth_home, Path) else None,
            container_auth_home=container_auth_home if isinstance(container_auth_home, str) else None,
        )
    return proxies


def _load_model_proxy_server(data: dict, config_dir: Path) -> ModelProxyServerConfig:
    raw = data.get("model_proxy_server", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail("[model_proxy_server] must be a table when present")
    host = _runtime_string(raw, "host", default="0.0.0.0", table="model_proxy_server")
    port = raw.get("port", 8787)
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        _fail("model_proxy_server.port must be between 0 and 65535")
    log_root = _runtime_optional_path(raw, "log_root", config_dir, table="model_proxy_server")
    body_logging = _runtime_string(raw, "body_logging", default="metadata", table="model_proxy_server")
    if body_logging not in {"none", "metadata", "full"}:
        _fail("model_proxy_server.body_logging must be none, metadata, or full")
    return ModelProxyServerConfig(host=host, port=port, log_root=log_root, body_logging=body_logging)


def _validate_resource_proxy_refs(
    resources: dict[str, ResourceConfig],
    proxies: dict[str, ModelProxyConfig],
) -> None:
    for name, resource in resources.items():
        if resource.proxy is not None and resource.proxy not in proxies:
            _fail(f"resources.{name}.proxy references unknown model_proxy {resource.proxy!r}")


def _validate_worker_resource_compatibility(
    runtime: RuntimeConfig,
    resources: dict[str, ResourceConfig],
    proxies: dict[str, ModelProxyConfig],
) -> None:
    allowed_proxy_kinds = {
        "codex": {"openai", "subscription"},
        "opencode": {"local", "openai"},
    }
    for worker_id, resource_ids in runtime.worker_resources.items():
        worker_type = runtime.worker_types.get(worker_id)
        if worker_type is None:
            _fail(f"runtime.worker_types.{worker_id} is required when worker resources are configured")
        allowed = allowed_proxy_kinds.get(worker_type)
        if allowed is None:
            _fail(f"runtime.worker_types.{worker_id} has unsupported worker type {worker_type!r}")
        for resource_id in resource_ids:
            resource = resources.get(resource_id)
            if resource is None:
                _fail(f"runtime.worker_resources.{worker_id} references unknown resource {resource_id!r}")
            if resource.proxy is None:
                continue
            proxy = proxies[resource.proxy]
            if proxy.kind not in allowed:
                _fail(
                    f"runtime.worker_resources.{worker_id} uses model_proxy kind "
                    f"{proxy.kind!r}, but worker type {worker_type!r} requires one of {sorted(allowed)!r}"
                )


def _required_table_string(raw: dict, key: str, table: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        _fail(f"{table}.{key} must be a non-empty string")
    return value.strip()


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


def _runtime_string_map_map(value: object, table: str) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(f"{table} must be a table")
    result: dict[str, dict[str, str]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            _fail(f"{table} keys must be non-empty strings")
        result[key.strip()] = _runtime_string_map(item, f"{table}.{key}")
    return result


def _resolve_config_path(value: str, config_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve()


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
