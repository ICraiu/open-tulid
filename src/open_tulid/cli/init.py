from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME

console = Console()


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

    content = (
        "tracker:\n"
        "  type: obsidian\n"
        "  root: /path/to/tracker/root\n"
        "\n"
        "projects:\n"
        "  Agent:\n"
        "    path: Agent\n"
        "    repo_root: /path/to/code/repository\n"
        "    main_branch: main\n"
        "\n"
        "runtime:\n"
        "  docker_executable: docker\n"
        "  shared_workspace_root: workspaces\n"
        "  container_workspace: /workspace/project\n"
        "  image_tag_prefix: open-tulid/agent\n"
        "  default_timeout_seconds: 3600\n"
        "  completion_host: 0.0.0.0\n"
        "  completion_port: 0\n"
        "  completion_container_host: host.docker.internal\n"
        "  container_volume_relabel: false\n"
        "  worker_images: {}\n"
        "  worker_args: {}\n"
        "  worker_resources: {}\n"
        "  worker_types: {}\n"
        "  env: {}\n"
    )
    config_path.write_text(content, encoding="utf-8")

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
