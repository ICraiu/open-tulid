from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

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
from open_tulid.domain import EventActor, EventType, ExecutionJobStatus, WorkflowDefinition
from open_tulid.models import Config, ProjectConfig, ValidationReport
from open_tulid.runtime import (
    ArtifactSubmission,
    CompletionService,
    CompletionEndpointConfig,
    CompletionSubmission,
    CreateExecutionJob,
    FileExecutionJobStore,
    JobExecutor,
    JsonlEventStore,
    RequestTransition,
    Scheduler,
    TaskManager,
    build_event,
    cleanup_job_workspaces,
    serve_completion_endpoint,
    TransactionJournalStore,
    human_event_type,
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

tasks_app = typer.Typer()
app.add_typer(tasks_app, name="tasks")

agents_app = typer.Typer()
app.add_typer(agents_app, name="agents")

install_app = typer.Typer()
app.add_typer(install_app, name="install")

scheduler_app = typer.Typer()
app.add_typer(scheduler_app, name="scheduler")


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


@app.command("validate")
def validate_alias() -> None:
    """Validate all configured projects in the vault."""
    validate()


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
            f">>> {event.timestamp} {human_event_type(event.event_type)} id={event.event_id}"
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


@app.command("log")
def log_events(
    lines: int | None = typer.Argument(None, min=1, help="Number of recent human log lines to print."),
    project: str | None = typer.Option(None, "--project", help="Configured project name."),
) -> None:
    """Tail human-readable project events, or follow them when no line count is given."""
    config = _load_cli_config()
    project_name = _resolve_project_name(config, project)
    log_dir = _project_path(config, project_name) / "events"
    if lines is not None:
        for line in _tail_human_logs(log_dir, lines):
            console.print(line)
        return
    _follow_human_logs(log_dir)


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
    ctx["event_store"].append_many(result.events)
    console.print(
        f"Scheduled job={result.job.job_id} task={result.job.task_id} "
        f"transition={result.job.transition_id} worker={result.job.worker_id}"
    )


@jobs_app.command("create")
def create_job(
    project: str = typer.Argument(..., help="Configured project name."),
    task_id: str = typer.Argument(..., help="Task id."),
    transition_id: str = typer.Argument(..., help="Transition id."),
) -> None:
    """Create an execution job for a specific task transition."""
    ctx = _runtime_project_context(project)
    manager = TaskManager(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
    )
    result = manager.create_execution_job(CreateExecutionJob(
        project_id=project,
        task_id=task_id,
        transition_id=transition_id,
        workspace_root=ctx["workspace_root"],
    ))
    if not result.accepted or result.job is None:
        _print_domain_errors(result.errors)
        raise typer.Exit(1)
    ctx["event_store"].append_many(result.events)
    console.print(f"Created job={result.job.job_id}")


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
    for key in (
        "completion_endpoint",
        "completion_endpoint_host",
        "completion_endpoint_port",
        "worker_returncode",
        "last_verification",
        "status_reason",
    ):
        value = job.metadata.get(key)
        if value is not None:
            console.print(f"{key}: {value}")


@jobs_app.command("status")
def jobs_status(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """Show execution job counts and active runtime metadata."""
    ctx = _runtime_project_context(project)
    listed = ctx["job_store"].list()
    if not listed.accepted:
        _print_domain_errors((listed.error,))
        raise typer.Exit(1)
    counts: dict[str, int] = {}
    for job in listed.jobs:
        status = _status(job.status)
        counts[status] = counts.get(status, 0) + 1
    if not counts:
        console.print("No jobs.")
        return
    console.print(" ".join(f"{key}={counts[key]}" for key in sorted(counts)))
    for job in listed.jobs:
        if _status(job.status) in {"pending", "running", "completion_rejected"}:
            endpoint = job.metadata.get("completion_endpoint")
            endpoint_part = f" endpoint={endpoint}" if endpoint else ""
            console.print(
                f"{job.job_id} status={_status(job.status)} task={job.task_id}"
                f" worker={job.worker_id}{endpoint_part}"
            )


@jobs_app.command("fail")
def fail_job(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
    reason: str = typer.Option("", "--reason", help="Failure reason."),
) -> None:
    """Mark a job failed."""
    _set_job_status(project, job_id, ExecutionJobStatus.FAILED, reason=reason)


@jobs_app.command("cancel")
def cancel_job(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
    reason: str = typer.Option("", "--reason", help="Cancellation reason."),
) -> None:
    """Mark a job cancelled."""
    _set_job_status(project, job_id, ExecutionJobStatus.CANCELLED, reason=reason)


@jobs_app.command("restart")
def restart_job(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
) -> None:
    """Return a job to pending."""
    _set_job_status(project, job_id, ExecutionJobStatus.PENDING, reason="restart requested")


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
        journal_store=ctx["journal_store"],
        artifact_root=ctx["project_path"] / "artifacts",
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
    ctx["event_store"].append_many(scheduled.events)
    run_job(project, scheduled.job.job_id)


@jobs_app.command("daemon")
def jobs_daemon(
    project: str = typer.Argument(..., help="Configured project name."),
    interval: float = typer.Option(30.0, "--interval", min=0.1, help="Seconds between scheduler scans."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum jobs to run before exiting."),
    exit_when_idle: bool = typer.Option(False, "--exit-when-idle", help="Exit after a scan finds no runnable task."),
) -> None:
    """Continuously schedule and run runnable jobs."""
    completed = 0
    while True:
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
            if exit_when_idle:
                return
            time.sleep(interval)
            continue

        ctx["event_store"].append_many(scheduled.events)
        console.print(
            f"Scheduled job={scheduled.job.job_id} task={scheduled.job.task_id} "
            f"transition={scheduled.job.transition_id} worker={scheduled.job.worker_id}"
        )
        run_job(project, scheduled.job.job_id)
        completed += 1
        if limit is not None and completed >= limit:
            return


@scheduler_app.command("start")
def scheduler_start(
    project: str | None = typer.Option(None, "--project", help="Configured project name."),
    interval: float = typer.Option(30.0, "--interval", min=0.1, help="Seconds between scheduler scans."),
) -> None:
    """Start the scheduler as a detached background process."""
    config = _load_cli_config()
    project_name = _resolve_project_name(config, project)
    state_path = _scheduler_state_path(config, project_name)
    state = _load_scheduler_state(state_path)
    if state is not None and _pid_is_running(state.get("pid")):
        console.print(f"Scheduler already running for {project_name} pid={state['pid']}")
        return

    process = subprocess.Popen(
        [sys.argv[0], "jobs", "daemon", project_name, "--interval", str(interval)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "pid": process.pid,
        "project": project_name,
        "interval": interval,
        "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }, sort_keys=True), encoding="utf-8")
    console.print(f"Scheduler started for {project_name} pid={process.pid}")


@scheduler_app.command("stop")
def scheduler_stop(
    project: str | None = typer.Option(None, "--project", help="Configured project name."),
) -> None:
    """Stop the detached scheduler process."""
    config = _load_cli_config()
    project_name = _resolve_project_name(config, project)
    state_path = _scheduler_state_path(config, project_name)
    state = _load_scheduler_state(state_path)
    if state is None or not _pid_is_running(state.get("pid")):
        state_path.unlink(missing_ok=True)
        console.print(f"Scheduler is not running for {project_name}")
        return
    os.kill(int(state["pid"]), signal.SIGTERM)
    state_path.unlink(missing_ok=True)
    console.print(f"Scheduler stopped for {project_name}")


@scheduler_app.command("status")
def scheduler_status(
    project: str | None = typer.Option(None, "--project", help="Configured project name."),
) -> None:
    """Show whether the detached scheduler is running."""
    config = _load_cli_config()
    project_name = _resolve_project_name(config, project)
    state_path = _scheduler_state_path(config, project_name)
    state = _load_scheduler_state(state_path)
    if state is None or not _pid_is_running(state.get("pid")):
        console.print(f"Scheduler not running for {project_name}")
        return
    console.print(f"Scheduler running for {project_name} pid={state['pid']}")


@jobs_app.command("complete")
def complete_job(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str = typer.Argument(..., help="Execution job id."),
    token: str | None = typer.Option(None, "--token", help="Completion token from the job context."),
    submission_id: str | None = typer.Option(None, "--submission-id", help="Caller supplied submission id for replay detection."),
    attempt: int | None = typer.Option(None, "--attempt", min=1, help="Completion attempt number."),
    summary: str = typer.Option("", "--summary", help="Completion summary."),
    artifact: list[str] | None = typer.Option(
        None,
        "--artifact",
        help="Submitted artifact as type=path or type=path:sha256. Repeatable.",
    ),
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
        journal_store=ctx["journal_store"],
        artifact_root=ctx["project_path"] / "artifacts",
    )
    result = service.submit(
        job_id=job_id,
        token=token,
        submission=CompletionSubmission(
            submission_id=submission_id,
            attempt=attempt,
            summary=summary,
            artifacts=_parse_artifacts(artifact or ()),
            changed_files=tuple(changed_file or ()),
            validation_evidence=_parse_evidence(evidence or ()),
        ),
    )
    if not result.accepted:
        _print_domain_errors(result.errors)
        raise typer.Exit(1)
    console.print("Completion accepted.")


@jobs_app.command("serve-completions")
def serve_completions(
    project: str = typer.Argument(..., help="Configured project name."),
    job_id: str | None = typer.Option(None, "--job", help="Restrict the endpoint to one job id."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind."),
    port: int = typer.Option(8765, "--port", min=0, help="Port to bind. Use 0 for an ephemeral port."),
) -> None:
    """Serve the local completion HTTP endpoint."""
    ctx = _runtime_project_context(project)
    service = CompletionService(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
        event_store=ctx["event_store"],
        journal_store=ctx["journal_store"],
        artifact_root=ctx["project_path"] / "artifacts",
    )
    server = serve_completion_endpoint(
        CompletionEndpointConfig(
            service=service,
            allowed_jobs=frozenset({job_id}) if job_id is not None else None,
        ),
        host=host,
        port=port,
    )
    bound_host, bound_port = server.server_address
    console.print(f"Serving completion endpoint at http://{bound_host}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


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


@tasks_app.command("list")
def list_tasks(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """List tasks from the configured project."""
    ctx = _runtime_project_context(project)
    loaded = ctx["adapter"].load_project()
    if not loaded.accepted or loaded.snapshot is None:
        _print_domain_errors(loaded.errors)
        raise typer.Exit(1)
    for task in sorted(loaded.snapshot.tasks.values(), key=lambda item: item.id):
        console.print(f"{task.id} state={task.current_state} type={task.task_type} title={task.title}")


@tasks_app.command("runnable")
def runnable_tasks(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """List tasks with worker-backed outgoing transitions."""
    ctx = _runtime_project_context(project)
    loaded = ctx["adapter"].load_project()
    if not loaded.accepted or loaded.snapshot is None:
        _print_domain_errors(loaded.errors)
        raise typer.Exit(1)
    found = False
    for task in sorted(loaded.snapshot.tasks.values(), key=lambda item: item.id):
        transitions = [
            transition for transition in ctx["workflow"].transitions.values()
            if transition.task_type == task.task_type
            and transition.from_state == task.current_state
            and transition.worker is not None
        ]
        for transition in transitions:
            found = True
            console.print(f"{task.id} transition={transition.id} worker={transition.worker}")
    if not found:
        console.print("No runnable tasks.")


@app.command("transition")
def transition_task(
    project: str = typer.Argument(..., help="Configured project name."),
    task_id: str = typer.Argument(..., help="Task id."),
    transition_id: str = typer.Argument(..., help="Transition id."),
) -> None:
    """Apply a trusted manual transition."""
    ctx = _runtime_project_context(project)
    manager = TaskManager(workflow=ctx["workflow"], adapter=ctx["adapter"])
    checked = manager.request_transition(RequestTransition(
        project_id=project,
        task_id=task_id,
        transition_id=transition_id,
        actor=EventActor(type="user", id="cli"),
    ))
    if not checked.accepted:
        _print_domain_errors(checked.errors)
        raise typer.Exit(1)
    transition = ctx["workflow"].transitions[transition_id]
    moved = ctx["adapter"].move_task(task_id, transition.to_state)
    if not moved.accepted:
        _print_domain_errors(moved.errors)
        raise typer.Exit(1)
    ctx["event_store"].append(build_event(
        project_id=project,
        actor=EventActor(type="user", id="cli"),
        event_type=EventType.TaskMoved,
        correlation_id=task_id,
        task_id=task_id,
        transition_id=transition_id,
        data={"to_state": transition.to_state, "path": moved.path},
    ))
    console.print(f"Moved {task_id} to {transition.to_state}.")


@jobs_app.command("cleanup")
def cleanup_jobs(
    project: str = typer.Argument(..., help="Configured project name."),
) -> None:
    """Remove workspaces for terminal jobs."""
    ctx = _runtime_project_context(project)
    listed = ctx["job_store"].list()
    if not listed.accepted:
        _print_domain_errors((listed.error,))
        raise typer.Exit(1)
    result = cleanup_job_workspaces(listed.jobs)
    if not result.accepted:
        _print_domain_errors(result.errors)
        raise typer.Exit(1)
    for path in result.removed:
        console.print(f"Removed {path}")
    if not result.removed:
        console.print("No terminal job workspaces to remove.")


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
        "journal_store": TransactionJournalStore(project_path / "events" / "journals"),
        "project_path": project_path,
        "workspace_root": default_shared_workspace_root(config.runtime, project_path),
    }


def _print_domain_errors(errors) -> None:
    for error in errors:
        if error is None:
            continue
        location = f" [{error.location}]" if error.location else ""
        console.print(f"[red]{error.code}[/red]{location}: {error.message}")


def _set_job_status(project: str, job_id: str, status: ExecutionJobStatus, *, reason: str = "") -> None:
    ctx = _runtime_project_context(project)
    updated = ctx["job_store"].update_status(
        job_id,
        status,
        metadata={"status_reason": reason} if reason else None,
    )
    if not updated.accepted or updated.job is None:
        _print_domain_errors((updated.error,))
        raise typer.Exit(1)
    event_type = EventType.ExecutionFailed if status == ExecutionJobStatus.FAILED else "ExecutionJobStatusChanged"
    ctx["event_store"].append(build_event(
        project_id=project,
        actor=EventActor(type="user", id="cli"),
        event_type=event_type,
        correlation_id=job_id,
        job_id=job_id,
        task_id=updated.job.task_id,
        transition_id=updated.job.transition_id,
        data={"status": status.value, "reason": reason},
    ))
    console.print(f"{job_id} status={status.value}")


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


def _parse_artifacts(values) -> tuple[ArtifactSubmission, ...]:
    artifacts: list[ArtifactSubmission] = []
    for value in values:
        if "=" not in value:
            clean = value.strip()
            if not clean:
                continue
            artifacts.append(ArtifactSubmission(type=clean, path=clean))
            continue
        artifact_type, rest = value.split("=", 1)
        path, sep, sha256 = rest.partition(":")
        if not artifact_type.strip() or not path.strip():
            console.print(Panel(f"Artifact must use type=path or type=path:sha256: {value}", style="red"))
            raise typer.Exit(2)
        artifacts.append(ArtifactSubmission(
            type=artifact_type.strip(),
            path=path.strip(),
            sha256=sha256.strip() if sep and sha256.strip() else None,
        ))
    return tuple(artifacts)


def _status(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _resolve_project_name(config: Config, project: str | None) -> str:
    if project is not None:
        _project_path(config, project)
        return project
    if len(config.projects) == 1:
        return config.projects[0]
    console.print(Panel("Multiple projects configured. Pass --project.", style="red"))
    raise typer.Exit(2)


def _tail_human_logs(log_dir: Path, lines: int) -> tuple[str, ...]:
    gathered: list[str] = []
    for path in sorted(log_dir.glob("*.log")):
        gathered.extend(path.read_text(encoding="utf-8").splitlines())
    return tuple(gathered[-lines:])


def _follow_human_logs(log_dir: Path, *, poll_interval: float = 0.2) -> None:
    positions = {
        path: path.stat().st_size
        for path in sorted(log_dir.glob("*.log"))
    }
    try:
        while True:
            for path in sorted(log_dir.glob("*.log")):
                offset = positions.get(path, 0)
                with path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    for line in handle:
                        console.print(line.rstrip("\n"))
                    positions[path] = handle.tell()
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        return


def _scheduler_state_path(config: Config, project: str) -> Path:
    return _project_path(config, project) / ".open-tulid" / "scheduler.json"


def _load_scheduler_state(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_is_running(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
