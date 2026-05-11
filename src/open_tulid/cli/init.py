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
        "[workflow]\n"
        "path = \"workflow.yaml\"\n"
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
        "statements:\n"
        "  - kind: state\n"
        "    id: Todo\n"
    )
