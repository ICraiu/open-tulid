from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from open_tulid.cli.init import init as init_cmd
from open_tulid.cli.uninstall import _do_uninstall
from open_tulid.config import load_config
from open_tulid.models import Config, ValidationReport
from open_tulid.vault.project import create_project
from open_tulid.vault.validator import validate_vault
from open_tulid.workflow.runtime import load_workflow_definition

app = typer.Typer(
    name="tulid",
    help="CLI tool for managing Obsidian vault projects.",
)

console = Console()


def _load_cli_config() -> Config:
    config = load_config()
    if config.workflow_path is None:
        return config

    workflow = load_workflow_definition(config.workflow_path)
    if workflow.valid:
        return config

    console.print(Panel("Workflow validation failed.", style="red"))
    for diagnostic in workflow.diagnostics:
        parts = []
        if getattr(diagnostic, "path", None):
            parts.append(str(diagnostic.path))
        if getattr(diagnostic, "line", None) is not None:
            parts.append(str(diagnostic.line))
        prefix = ":".join(parts)
        message = f"{diagnostic.code}: {diagnostic.message}"
        console.print(f"{prefix}: {message}" if prefix else message)
    raise typer.Exit(2)


@app.command()
def init() -> None:
    """Create ~/.tuluid/open-tulid.toml configuration file."""
    init_cmd()


@app.command()
def project(
    name: str = typer.Argument(..., help="Name of the new project to create."),
) -> None:
    """Create a new project directory inside the configured vault."""
    config = _load_cli_config()
    result = create_project(config, name)
    for dir_path in result.created_dirs:
        console.print(f"Created {dir_path}")
    console.print(f"Project created: {result.name}")


vault_app = typer.Typer()
app.add_typer(vault_app, name="vault")


@app.command()
def uninstall() -> None:
    """Uninstall open-tulid from the current environment."""
    _do_uninstall()


@vault_app.command()
def validate() -> None:
    """Validate all configured projects in the vault."""
    config = _load_cli_config()
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
