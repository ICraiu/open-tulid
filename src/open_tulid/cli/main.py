from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from open_tulid.cli.init import init as init_cmd
from open_tulid.cli.uninstall import _do_uninstall
from open_tulid.config import load_config
from open_tulid.containers import (
    build_agent_images,
    check_docker,
    default_shared_workspace_root,
    docker_install_plan,
    list_agent_image_specs,
)
from open_tulid.adapters.obsidian import ObsidianAdapter, config_from_workflow
from open_tulid.domain import WorkflowDefinition
from open_tulid.models import Config, ProjectConfig, ValidationReport
from open_tulid.runtime import (
    CompletionService,
    CompletionSubmission,
    FileExecutionJobStore,
    JobExecutor,
    JsonlEventStore,
    Scheduler,
    TransactionJournalStore,
)
from open_tulid.vault.project import create_project
from open_tulid.vault.validator import validate_vault
from open_tulid.workflow.runtime import load_workflow_definition

app = typer.Typer(
    name="tulid",
    help="CLI tool for managing Obsidian vault projects.",
)

console = Console()


def _load_cli_context() -> tuple[Config, WorkflowDefinition | None]:
    config = load_config()
    if config.workflow_path is None:
        return config, None

    workflow = load_workflow_definition(config.workflow_path)
    if workflow.valid:
        return config, workflow.definition

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


def _load_cli_config() -> Config:
    config, _workflow_definition = _load_cli_context()
    return config


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

jobs_app = typer.Typer()
app.add_typer(jobs_app, name="jobs")

agents_app = typer.Typer()
app.add_typer(agents_app, name="agents")

install_app = typer.Typer()
app.add_typer(install_app, name="install")


@app.command()
def uninstall() -> None:
    """Uninstall open-tulid from the current environment."""
    _do_uninstall()


@agents_app.command("build-images")
def build_agent_images_cmd(
    agent: list[str] | None = typer.Option(
        None,
        "--agent",
        help="Agent image to build. May be provided more than once. Defaults to all agents.",
    ),
    tag_prefix: str = typer.Option(
        "open-tulid/agent",
        "--tag-prefix",
        help="Image tag prefix used for default tags.",
    ),
    docker: str = typer.Option(
        "docker",
        "--docker",
        help="Docker executable to invoke.",
    ),
) -> None:
    """Build local Docker images for coding agents."""
    known_agents = {spec.id for spec in list_agent_image_specs(tag_prefix=tag_prefix)}
    selected_agents = tuple(agent or sorted(known_agents))
    unknown = sorted(set(selected_agents) - known_agents)
    if unknown:
        console.print(Panel(
            f"Unknown agent image(s): {', '.join(unknown)}",
            style="red",
        ))
        raise typer.Exit(2)

    results = build_agent_images(
        selected_agents,
        tag_prefix=tag_prefix,
        docker_executable=docker,
    )
    failed = [result for result in results if not result.succeeded]
    for result in results:
        if result.succeeded:
            console.print(f"Built {result.agent_id}: {result.tag}")
        else:
            console.print(Panel(
                f"Failed to build {result.agent_id}: {result.tag}\n{result.stderr.strip()}",
                style="red",
            ))
    if failed:
        raise typer.Exit(1)


@agents_app.command("doctor")
def agents_doctor(
    docker: str = typer.Option(
        "docker",
        "--docker",
        help="Docker executable to check.",
    ),
) -> None:
    """Check whether the local Docker agent runtime is usable."""
    result = check_docker(docker)
    if result.available:
        console.print(Panel("Docker is available for agent execution.", style="green"))
        return
    reason = result.failure_reason or "docker_unavailable"
    console.print(Panel(f"Docker is not available: {reason}\n{result.error}", style="red"))
    raise typer.Exit(1)


@install_app.command("docker")
def install_docker(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--execute",
        help="Show the install plan by default. Use --execute only after reviewing it.",
    ),
) -> None:
    """Show or execute the Docker installation plan for this host."""
    plan = docker_install_plan()
    if not plan.supported:
        console.print(Panel(
            "\n".join(("Docker installation is not automated for this host.", *plan.notes)),
            style="red",
        ))
        raise typer.Exit(2)

    console.print(f"Platform: {plan.platform_id}")
    for note in plan.notes:
        console.print(f"- {note}")
    for command in plan.commands:
        console.print(" ".join(command))

    if dry_run:
        console.print(Panel("Dry run only. Re-run with --execute to run these commands.", style="yellow"))
        return

    import subprocess
    for command in plan.commands:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            console.print(Panel(
                f"Command failed with exit code {completed.returncode}: {' '.join(command)}",
                style="red",
            ))
            raise typer.Exit(completed.returncode)


@vault_app.command()
def validate() -> None:
    """Validate all configured projects in the vault."""
    config, workflow_definition = _load_cli_context()
    report = validate_vault(config, workflow_definition)
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


@jobs_app.command("schedule")
def schedule_job(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """Schedule the next runnable task and create one execution job."""
    ctx = _runtime_project_context(project)
    scheduler = Scheduler(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
        workspace_root=ctx["workspace_root"],
    )
    result = scheduler.schedule_one(project)
    if not result.accepted:
        _print_domain_errors(result.errors)
        raise typer.Exit(1)
    for skipped in result.skipped:
        console.print(f"[yellow]Skipped[/yellow] {skipped.code}: {skipped.message}")
    if not result.scheduled or result.job is None:
        console.print("No runnable task.")
        return
    console.print(
        f"Scheduled job={result.job.job_id} task={result.job.task_id} "
        f"transition={result.job.transition_id} worker={result.job.worker_id}"
    )


@jobs_app.command("list")
def list_jobs(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """List persisted execution jobs for a project."""
    ctx = _runtime_project_context(project)
    listed = ctx["job_store"].list()
    if not listed.accepted:
        _print_domain_errors((listed.error,))
        raise typer.Exit(1)
    if not listed.jobs:
        console.print("No jobs.")
        return
    for job in listed.jobs:
        console.print(
            f"{job.job_id} status={_status(job.status)} task={job.task_id} "
            f"transition={job.transition_id} worker={job.worker_id}"
        )


@jobs_app.command("show")
def show_job(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
) -> None:
    """Show one execution job."""
    ctx = _runtime_project_context(project)
    loaded = ctx["job_store"].get(job_id)
    if not loaded.accepted or loaded.job is None:
        _print_domain_errors((loaded.error,))
        raise typer.Exit(1)
    job = loaded.job
    console.print(f"job_id: {job.job_id}")
    console.print(f"status: {_status(job.status)}")
    console.print(f"task_id: {job.task_id}")
    console.print(f"transition_id: {job.transition_id}")
    console.print(f"worker_id: {job.worker_id}")
    console.print(f"workspace_path: {job.workspace_path}")
    console.print(f"attempts: {job.attempts}")


@jobs_app.command("run")
def run_job(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
) -> None:
    """Prepare the workspace and run the configured worker container."""
    ctx = _runtime_project_context(project)
    executor = JobExecutor(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
        event_store=ctx["event_store"],
        runtime=ctx["config"].runtime,
        project_config=ctx["project_config"],
    )
    result = executor.run(job_id)
    if not result.accepted:
        _print_domain_errors(result.errors)
        raise typer.Exit(1)
    if result.run is not None:
        console.print(f"Worker exited with code {result.run.returncode}.")
        if not result.run.succeeded:
            raise typer.Exit(result.run.returncode or 1)


@jobs_app.command("run-one")
def run_one_job(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """Schedule and run one runnable task."""
    ctx = _runtime_project_context(project)
    scheduler = Scheduler(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
        workspace_root=ctx["workspace_root"],
    )
    scheduled = scheduler.schedule_one(project)
    if not scheduled.accepted:
        _print_domain_errors(scheduled.errors)
        raise typer.Exit(1)
    if not scheduled.scheduled or scheduled.job is None:
        console.print("No runnable task.")
        return
    run_job(project, scheduled.job.job_id)


@jobs_app.command("complete")
def complete_job(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
    token: str | None = typer.Option(None, "--token", help="Completion token from the job context."),
    summary: str = typer.Option("", "--summary", help="Completion summary."),
    artifact: list[str] | None = typer.Option(None, "--artifact", help="Submitted artifact path. Repeatable."),
    changed_file: list[str] | None = typer.Option(None, "--changed-file", help="Changed file path. Repeatable."),
    evidence: list[str] | None = typer.Option(
        None,
        "--evidence",
        help="Validation evidence in validation_id=value form. Repeatable.",
    ),
) -> None:
    """Submit completion evidence through the trusted verifier."""
    ctx = _runtime_project_context(project)
    service = CompletionService(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
        event_store=ctx["event_store"],
    )
    result = service.submit(
        job_id=job_id,
        token=token,
        submission=CompletionSubmission(
            summary=summary,
            artifacts=tuple(artifact or ()),
            changed_files=tuple(changed_file or ()),
            validation_evidence=_parse_evidence(evidence or ()),
        ),
    )
    if not result.accepted:
        _print_domain_errors(result.errors)
        raise typer.Exit(1)
    console.print("Completion accepted.")


@jobs_app.command("logs")
def job_logs(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
    stream: str = typer.Option("stdout", "--stream", help="stdout, stderr, or command."),
) -> None:
    """Print persisted worker logs for one job."""
    ctx = _runtime_project_context(project)
    loaded = ctx["job_store"].get(job_id)
    if not loaded.accepted or loaded.job is None:
        _print_domain_errors((loaded.error,))
        raise typer.Exit(1)
    name = {"stdout": "stdout.log", "stderr": "stderr.log", "command": "command.txt"}.get(stream)
    if name is None:
        console.print(Panel("stream must be stdout, stderr, or command", style="red"))
        raise typer.Exit(2)
    path = loaded.job.workspace_path
    log_path = __import__("pathlib").Path(path) / ".open-tulid" / "logs" / name
    if not log_path.is_file():
        console.print("No log file.")
        return
    console.print(log_path.read_text(encoding="utf-8"), end="")


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


def _runtime_project_context(project: str) -> dict[str, object]:
    config, workflow = _load_cli_context()
    if workflow is None:
        console.print(Panel("workflow.path is required for runtime job commands.", style="red"))
        raise typer.Exit(2)
    project_path = _project_path(config, project)
    project_config = config.project_configs.get(project) or ProjectConfig(
        name=project,
        tracker_path=project,
    )
    adapter = ObsidianAdapter(config_from_workflow(
        project_id=project,
        project_root=project_path,
        workflow=workflow,
    ))
    return {
        "config": config,
        "workflow": workflow,
        "project_config": project_config,
        "adapter": adapter,
        "job_store": FileExecutionJobStore(project_path / "jobs"),
        "event_store": JsonlEventStore(project_path / "events"),
        "workspace_root": default_shared_workspace_root(config.runtime, project_path),
    }


def _print_domain_errors(errors) -> None:
    for error in errors:
        if error is None:
            continue
        location = f" [{error.location}]" if error.location else ""
        console.print(f"[red]{error.code}[/red]{location}: {error.message}")


def _parse_evidence(values) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            console.print(Panel(f"Evidence must use validation_id=value: {value}", style="red"))
            raise typer.Exit(2)
        key, item = value.split("=", 1)
        if not key.strip() or not item.strip():
            console.print(Panel(f"Evidence must use validation_id=value: {value}", style="red"))
            raise typer.Exit(2)
        parsed[key.strip()] = item.strip()
    return parsed


def _status(status) -> str:
    return status.value if hasattr(status, "value") else str(status)
