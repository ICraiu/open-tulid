from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME
from open_tulid.workflow.runtime import load_workflow_definition

console = Console()


def init() -> None:
    """Create ~/.tuluid/open-tulid.toml configuration file."""
    config_dir = Path.home() / CONFIG_DIRNAME
    config_path = config_dir / CONFIG_FILENAME
    workflow_path = config_dir / "workflow.yaml"

    if config_path.exists():
        console.print(Panel(
            f"Config already exists at {config_path}",
            style="yellow",
        ))
        raise SystemExit(1)
    if workflow_path.exists():
        console.print(Panel(
            f"Workflow already exists at {workflow_path}",
            style="yellow",
        ))
        raise SystemExit(1)

    config_dir.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(_default_workflow(), encoding="utf-8")

    content = (
        "[vault]\n"
        "root = \"/path/to/obsidian/vault\"\n"
        "projects = [\"Agent\", \"Game\"]\n"
        "\n"
        "[projects.Agent]\n"
        "tracker_path = \"Agent\"\n"
        "repo_root = \"/path/to/code/repository\"\n"
        "main_branch = \"main\"\n"
        "\n"
        "[projects.Game]\n"
        "tracker_path = \"Game\"\n"
        "repo_root = \"/path/to/another/code/repository\"\n"
        "main_branch = \"main\"\n"
        "\n"
        "[workflow]\n"
        "path = \"workflow.yaml\"\n"
        "\n"
        "[runtime]\n"
        "docker_executable = \"docker\"\n"
        "shared_workspace_root = \"workspaces\"\n"
        "container_workspace = \"/workspace/project\"\n"
        "image_tag_prefix = \"open-tulid/agent\"\n"
        "default_timeout_seconds = 3600\n"
        "completion_host = \"0.0.0.0\"\n"
        "completion_port = 0\n"
        "completion_container_host = \"host.docker.internal\"\n"
        "\n"
        "[runtime.worker_images]\n"
        "# codex = \"open-tulid/agent-codex:latest\"\n"
        "# opencode = \"open-tulid/agent-opencode:latest\"\n"
        "\n"
        "[runtime.worker_args]\n"
        "# codex = [\"exec\", \"--full-auto\", \"--\", \"Read {prompt_packet} and complete the job.\"]\n"
        "# opencode = [\"run\", \"Read {prompt_packet} and complete the job.\"]\n"
        "\n"
        "[runtime.env]\n"
    )
    config_path.write_text(content, encoding="utf-8")
    workflow_result = load_workflow_definition(workflow_path)
    if not workflow_result.valid:
        console.print(Panel("Default workflow validation failed.", style="red"))
        for diagnostic in workflow_result.diagnostics:
            console.print(f"{diagnostic.code}: {diagnostic.message}")
        raise SystemExit(2)

    console.print(Panel(
        f"Config created at {config_path}",
        style="green",
    ))


def _default_workflow() -> str:
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
    )
