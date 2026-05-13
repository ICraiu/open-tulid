from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from open_tulid.cli.init import init as init_cmd
from open_tulid.cli.uninstall import _do_uninstall
from open_tulid.config import load_config
from open_tulid.models import Config, ValidationReport
from open_tulid.runtime import JsonlEventStore, TransactionJournalStore
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

events_app = typer.Typer()
app.add_typer(events_app, name="events")


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


@events_app.command("list")
def list_events(
    project: str = typer.Argument(..., help="Configured project name."),
    task: str | None = typer.Option(None, "--task", help="Only show events for one task id."),
    limit: int = typer.Option(20, "--limit", min=1, help="Maximum number of events to show."),
) -> None:
    """List recent structured events for a project."""
    project_path = _project_path(_load_cli_config(), project)
    records = JsonlEventStore(project_path / "events").iter_event_records()
    events = [
        record.event
        for record in records
        if record.event is not None and (task is None or record.event.task_id == task)
    ]
    for record in records:
        if record.error is not None:
            console.print(
                f"[yellow]Skipped corrupt event[/yellow] "
                f"{record.error.location}: {record.error.message}"
            )
    if not events:
        console.print("No events.")
        return
    for event in events[-limit:]:
        task_part = f" task={event.task_id}" if event.task_id else ""
        transition_part = f" transition={event.transition_id}" if event.transition_id else ""
        console.print(
            f"{event.timestamp} {event.event_type} id={event.event_id}"
            f"{task_part}{transition_part}"
        )


@events_app.command("status")
def event_status(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """Show transaction journal state for a project."""
    project_path = _project_path(_load_cli_config(), project)
    journals = TransactionJournalStore(project_path / "events" / "journals").iter_journals()
    counts = {"prepared": 0, "committed": 0, "failed": 0}
    for journal in journals:
        counts[str(journal.status.value if hasattr(journal.status, "value") else journal.status)] += 1
    console.print(
        f"prepared={counts['prepared']} committed={counts['committed']} failed={counts['failed']}"
    )
    for journal in journals:
        status = str(journal.status.value if hasattr(journal.status, "value") else journal.status)
        if status in {"prepared", "failed"}:
            suffix = f" task={journal.task_id}" if journal.task_id else ""
            console.print(f"{status} {journal.journal_id}{suffix}")


def _project_path(config: Config, name: str):
    if name not in config.projects:
        console.print(Panel(f"Project is not configured: {name}", style="red"))
        raise typer.Exit(2)
    project_config = config.project_configs.get(name)
    tracker_path = project_config.tracker_path if project_config is not None else name
    path = config.vault_root / tracker_path
    if not path.is_dir():
        console.print(Panel(f"Project directory does not exist: {path}", style="red"))
        raise typer.Exit(2)
    return path
