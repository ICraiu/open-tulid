from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from open_tulid.config import CONFIG_FILENAME, load_config
from open_tulid.models import ValidationReport
from open_tulid.vault.project import create_project, iter_configured_projects
from open_tulid.vault.validator import validate_vault

app = typer.Typer(
    name="tulid",
    help="CLI tool for managing Obsidian vault projects.",
)

console = Console()


def _get_config() -> object:
    return load_config()


@app.command()
def project(
    name: str = typer.Argument(..., help="Name of the new project to create."),
) -> None:
    """Create a new project directory inside the configured vault."""
    config = load_config()
    result = create_project(config, name)
    for dir_path in result.created_dirs:
        console.print(f"Created {dir_path}")
    console.print(f"Project created: {result.name}")


vault_app = typer.Typer()
app.add_typer(vault_app, name="vault")


@vault_app.command()
def validate() -> None:
    """Validate all configured projects in the vault."""
    config = load_config()
    report = validate_vault(config)
    _print_report(report)

    if report.passed:
        console.print(Panel("Vault validation passed.", style="green"))
        raise typer.Exit(0)
    else:
        console.print(Panel("Vault validation failed.", style="red"))
        raise typer.Exit(1)


def _print_report(report: ValidationReport) -> None:
    if not report.passed:
        console.print()
        for error in report.errors:
            parts = []
            if error.path is not None:
                parts.append(str(error.path))
            if error.line is not None:
                parts.append(str(error.line))
            prefix = ": ".join(parts) + ":" if parts else ""
            if prefix:
                console.print(f"  [dim]{prefix}[/dim]")
            console.print(f"    {error.message}")
        console.print()

    console.print(f"Checked {report.checked_projects} projects.")
    console.print(f"Checked {report.checked_kanban_files} kanban files.")
    console.print(f"Checked {report.checked_task_links} task links.")
