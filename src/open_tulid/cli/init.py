from __future__ import annotations

import sys
from pathlib import Path

import typer

from rich.console import Console
from rich.panel import Panel

console = Console()
app = typer.Typer(
    name="init",
    help="Create a configuration file for open-tulid.",
)


@app.command()
def init() -> None:
    """Interactively create ~/.open-tulid.toml configuration file."""
    config_path = Path.home() / ".open-tulid.toml"

    if config_path.exists():
        console.print(Panel(
            "Config already exists at " + str(config_path),
            style="yellow",
        ))
        raise SystemExit(1)

    vault_root = typer.prompt("Vault root directory path")
    project_names_input = typer.prompt(
        "Project names (comma-separated)",
        default="Agent,Game",
    )
    project_names = [p.strip() for p in project_names_input.split(",") if p.strip()]

    if not vault_root:
        console.print(Panel("Vault root cannot be empty.", style="red"))
        raise SystemExit(1)

    if not project_names:
        console.print(Panel("At least one project name is required.", style="red"))
        raise SystemExit(1)

    content = f"[vault]\nroot = \"{vault_root}\"\nprojects = {project_names!r}\n"
    config_path.write_text(content, encoding="utf-8")

    console.print(Panel(
        f"Config created at {config_path}\nVault: {vault_root}\nProjects: {', '.join(project_names)}",
        style="green",
    ))
