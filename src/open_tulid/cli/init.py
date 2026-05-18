from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME

console = Console()

BASE_CONFIG = """# Tulid stores tracker projects and machine-local runtime settings here.
tracker:
  # Tracker adapter to use. "obsidian" is the implemented adapter today.
  type: obsidian
  # Root directory containing Tulid-managed tracker projects.
  root: /path/to/tracker/root

# New projects can be added by running: tulid project <name>
projects: {}

runtime:
  # Command used to launch worker containers.
  docker_executable: docker
  # Relative paths here are resolved from ~/.tulid/.
  shared_workspace_root: workspaces
  container_workspace: /workspace/project
  image_tag_prefix: open-tulid/agent
  default_timeout_seconds: 3600
  completion_host: 0.0.0.0
  completion_port: 0
  completion_container_host: host.docker.internal
  container_volume_relabel: false
  worker_images: {}
  worker_args: {}
  worker_resources: {}
  worker_types: {}
  env: {}
"""


def init() -> None:
    """Create ~/.tulid/config.yaml."""
    config_dir = Path.home() / CONFIG_DIRNAME
    config_path = config_dir / CONFIG_FILENAME

    if config_path.exists():
        console.print(Panel(
            f"Config already exists at {config_path}",
            style="yellow",
        ))
        raise SystemExit(1)
    config_dir.mkdir(parents=True, exist_ok=True)

    config_path.write_text(BASE_CONFIG, encoding="utf-8")

    console.print(Panel(
        f"Config created at {config_path}",
        style="green",
    ))


def default_workflow() -> str:
    return (
        "schema_version: 1\n"
        "storage:\n"
        "  obsidian:\n"
        "    boards:\n"
        "      Work: kanban/Work.md\n"
        "    state_mappings:\n"
        "      - state: Todo\n"
        "        board: Work\n"
        "        column: Todo\n"
        "statements:\n"
        "  - kind: state\n"
        "    id: Todo\n"
        "  - kind: task_type\n"
        "    id: task\n"
    )
