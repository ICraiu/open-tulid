from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from open_tulid.models import Config


CONFIG_FILENAME = ".open-tulid.toml"


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def load_config(path: Path | None = None) -> Config:
    if path is None:
        cwd_config = Path.cwd() / CONFIG_FILENAME
        if cwd_config.is_file():
            path = cwd_config
        else:
            path = Path.home() / CONFIG_FILENAME

    if not path.is_file():
        _fail(f"Config file not found: {path}")

    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))

    vault = data.get("vault")
    if vault is None:
        _fail("[vault] section is missing from config")

    vault_root_str = vault.get("root")
    if vault_root_str is None:
        _fail("vault.root is missing from config")

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

    # Resolve and verify each project path stays within vault root
    abs_vault = vault_root.resolve()
    for name in validated_projects:
        candidate = (abs_vault / name).resolve()
        if not str(candidate).startswith(str(abs_vault) + os.sep) and candidate != abs_vault:
            _fail(f"Project name escapes vault root: {name}")

    return Config(vault_root=vault_root, projects=validated_projects)


def _validate_project_name(name: str) -> None:
    if "/" in name:
        _fail(f"Project name contains '/': {name}")
    if "\\" in name:
        _fail(f"Project name contains '\\': {name}")
    if ".." in name:
        _fail(f"Project name contains '..': {name}")
    if Path(name).is_absolute():
        _fail(f"Project name is an absolute path: {name}")
