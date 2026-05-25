from __future__ import annotations

import json
import os
import signal
import socket
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
from open_tulid.config import CONFIG_DIRNAME, load_config
from open_tulid.containers import (
    build_agent_images,
    check_docker,
    default_shared_workspace_root,
    docker_install_plan,
    list_agent_image_specs,
)
from open_tulid.adapters import AdapterBuildRequest, build_storage_adapter
from open_tulid.domain import EventActor, EventType, ExecutionJobStatus, WorkflowDefinition
from open_tulid.models import Config, ProjectConfig, ValidationReport
from open_tulid.runtime import (
    ArtifactSubmission,
    CompletionService,
    CompletionEndpointConfig,
    CompletionSubmission,
    CreateExecutionJob,
    FileExecutionJobStore,
    FileModelProxySessionStore,
    FileResourceLeaseStore,
    FileTransactionRuntime,
    JobExecutor,
    JsonlEventStore,
    LocalModelAdapter,
    ModelProxyService,
    OpenAIAdapter,
    RequestTransition,
    Scheduler,
    TaskManager,
    build_event,
    check_backend_readiness,
    cleanup_job_workspaces,
    recover_job_creation_transactions,
    recover_completion_transactions,
    serve_completion_endpoint,
    serve_model_proxy,
    TransactionJournalStore,
    human_event_type,
    new_ulid,
)
from open_tulid.vault.project import create_project
from open_tulid.vault.validator import validate_vault
from open_tulid.workflow.runtime import load_workflow_definition
from open_tulid.workflow.implementations import (
    OperationResult,
    VALIDATION_IMPLEMENTATIONS,
    WorkflowExecutionContext,
)

app = typer.Typer(
    name="tulid",
    help="CLI tool for managing tracker projects.",
)

console = Console()


def _load_cli_context(project: str | None = None) -> tuple[Config, WorkflowDefinition | None]:
    config = load_config()
    if project is None:
        return config, None
    project_config = config.project_configs.get(project)
    if project_config is None or project_config.workflow_path is None:
        return config, None
    workflow = load_workflow_definition(project_config.workflow_path)
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

runtime_app = typer.Typer()
model_proxy_app = typer.Typer()
transactions_app = typer.Typer()
app.add_typer(runtime_app, name="runtime")
app.add_typer(model_proxy_app, name="model-proxy")
app.add_typer(transactions_app, name="transactions")


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
        lease_store=ctx["lease_store"],
        worker_resources=ctx["config"].runtime.worker_resources,
        serial_repo_execution=ctx["config"].runtime.repo_execution_mode == "serial",
        event_store=ctx["event_store"],
        journal_store=ctx["journal_store"],
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
    if not result.events_persisted:
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
    transition = ctx["workflow"].transitions.get(transition_id)
    worker_id = transition.worker if transition is not None else None
    job_id = new_ulid()
    required_resources = ctx["config"].runtime.worker_resources.get(worker_id or "", ())
    scheduler = Scheduler(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
        workspace_root=ctx["workspace_root"],
        lease_store=ctx["lease_store"],
        worker_resources=ctx["config"].runtime.worker_resources,
        serial_repo_execution=ctx["config"].runtime.repo_execution_mode == "serial",
        event_store=ctx["event_store"],
        journal_store=ctx["journal_store"],
    )
    manager = TaskManager(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=None,
    )
    command = CreateExecutionJob(
        project_id=project,
        task_id=task_id,
        transition_id=transition_id,
        workspace_root=ctx["workspace_root"],
        job_id=job_id,
    )
    if required_resources:
        reserved, result = ctx["lease_store"].admit(
            required_resources,
            job_id=job_id,
            worker_id=worker_id or "",
            owner_path=ctx["job_store"].path_for(job_id),
            commit=lambda: scheduler._create_job(manager, command),
        )
        if not reserved.acquired:
            console.print(_runtime_log_line(
                "JOB_CREATE_DEFERRED",
                f"project={project} task={task_id} resources={','.join(reserved.busy_resources)}",
            ))
            raise typer.Exit(1)
        assert result is not None
    else:
        result = scheduler._create_job(manager, command)
    if not result.accepted or result.job is None:
        if required_resources:
            ctx["lease_store"].release_job(job_id)
        _print_domain_errors(result.errors)
        raise typer.Exit(1)
    console.print(_runtime_log_line("EXECUTION_JOB_CREATED", f"job={result.job.job_id}"))


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
        lease_store=ctx["lease_store"],
        resources=ctx["config"].resources,
        model_proxies=ctx["config"].model_proxy,
        model_proxy_sessions=ctx["model_proxy_sessions"],
        model_proxy_endpoint_base=_model_proxy_endpoint_base(ctx["config"]),
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=_validation_context,
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
        lease_store=ctx["lease_store"],
        worker_resources=ctx["config"].runtime.worker_resources,
        serial_repo_execution=ctx["config"].runtime.repo_execution_mode == "serial",
        event_store=ctx["event_store"],
        journal_store=ctx["journal_store"],
    )
    scheduled = scheduler.schedule_one(project)
    if not scheduled.accepted:
        _print_domain_errors(scheduled.errors)
        raise typer.Exit(1)
    if not scheduled.scheduled or scheduled.job is None:
        console.print("No runnable task.")
        return
    if not scheduled.events_persisted:
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
            lease_store=ctx["lease_store"],
            worker_resources=ctx["config"].runtime.worker_resources,
            serial_repo_execution=ctx["config"].runtime.repo_execution_mode == "serial",
            event_store=ctx["event_store"],
            journal_store=ctx["journal_store"],
        )
        scheduled = scheduler.schedule_one(project)
        if not scheduled.accepted:
            _print_domain_errors(scheduled.errors)
            raise typer.Exit(1)
        for skipped in scheduled.skipped:
            console.print(_runtime_log_line(
                "SCHEDULER_SKIPPED",
                f"project={project} code={skipped.code} message={skipped.message}",
            ))
        if not scheduled.scheduled or scheduled.job is None:
            console.print(_runtime_log_line("SCHEDULER_IDLE", f"project={project}"))
            if exit_when_idle:
                return
            time.sleep(interval)
            continue

        if not scheduled.events_persisted:
            ctx["event_store"].append_many(scheduled.events)
        console.print(_runtime_log_line(
            "SCHEDULER_SCHEDULED",
            f"project={project} job={scheduled.job.job_id} task={scheduled.job.task_id} "
            f"transition={scheduled.job.transition_id} worker={scheduled.job.worker_id}",
        ))
        try:
            run_job(project, scheduled.job.job_id)
        except typer.Exit as exc:
            console.print(_runtime_log_line(
                "JOB_RUN_FAILED",
                f"project={project} job={scheduled.job.job_id} exit_code={exc.exit_code}",
            ))
        except Exception as exc:
            console.print(_runtime_log_line(
                "JOB_RUN_FAILED",
                f"project={project} job={scheduled.job.job_id} error={exc}",
            ))
        completed += 1
        if limit is not None and completed >= limit:
            return


@runtime_app.command("start")
def runtime_start(
    project: str | None = typer.Option(None, "--project", help="Configured project name."),
    interval: float = typer.Option(30.0, "--interval", min=0.1, help="Seconds between scheduler scans."),
) -> None:
    """Start the runtime services as detached background processes."""
    config = _load_cli_config()
    projects = _resolve_projects(config, project)
    proxy_state_path = _proxy_state_path(config)
    proxy_state = _load_runtime_state(proxy_state_path)
    proxy_running = proxy_state is not None and _pid_is_running(proxy_state.get("proxy_pid"))
    scheduler_states: dict[str, tuple[Path, dict[str, object] | None, bool]] = {}
    for project_name in projects:
        state_path = _runtime_state_path(config, project_name)
        state = _load_runtime_state(state_path)
        scheduler_running = state is not None and _pid_is_running(state.get("scheduler_pid"))
        scheduler_states[project_name] = (state_path, state, scheduler_running)
        if state is not None and not scheduler_running:
            _fail_orphaned_runtime_jobs(config, project_name)
    if proxy_running and all(running for _, _, running in scheduler_states.values()):
        if len(projects) == 1:
            console.print(f"Runtime already running for {projects[0]}")
        else:
            console.print("Runtime already running for all selected projects")
        return

    readiness = check_backend_readiness(config.model_proxy, env=os.environ)
    for result in readiness:
        if result.ready:
            console.print(_runtime_log_line(
                "MODEL_PROXY_HEALTH_OK",
                f"proxy={result.proxy_id} status={result.status}",
            ))
        else:
            detail = f"proxy={result.proxy_id}"
            if result.status is not None:
                detail += f" status={result.status}"
            if result.error:
                detail += f" error={result.error}"
            console.print(_runtime_log_line("MODEL_PROXY_HEALTH_FAILED", detail))
    if any(not result.ready for result in readiness):
        raise typer.Exit(1)

    proxy_pid = None
    started_new_proxy = False
    if proxy_running:
        proxy_pid = int(proxy_state["proxy_pid"])
        console.print(_runtime_log_line("MODEL_PROXY_ALREADY_RUNNING", f"pid={proxy_pid}"))
    else:
        proxy_process = _spawn_runtime_process(
            config,
            "model-proxy",
            _self_cli_command("model-proxy", "serve"),
        )
        if not _process_survived_startup(proxy_process):
            console.print(_runtime_log_line("MODEL_PROXY_START_FAILED", f"pid={proxy_process.pid}"))
            raise typer.Exit(1)
        if not _proxy_listener_ready(config):
            console.print(_runtime_log_line("MODEL_PROXY_LISTENER_FAILED", f"pid={proxy_process.pid}"))
            _stop_new_proxy_after_failed_start(proxy_process, proxy_state_path)
            raise typer.Exit(1)
        proxy_pid = proxy_process.pid
        started_new_proxy = True
        proxy_state_path.parent.mkdir(parents=True, exist_ok=True)
        proxy_state_path.write_text(json.dumps({
            "proxy_pid": proxy_pid,
            "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }, sort_keys=True), encoding="utf-8")
        console.print(_runtime_log_line("MODEL_PROXY_STARTED", f"pid={proxy_pid}"))
    scheduler_failures = False
    active_schedulers = 0
    for project_name in projects:
        state_path, state, scheduler_running = scheduler_states[project_name]
        if scheduler_running:
            active_schedulers += 1
            console.print(_runtime_log_line(
                "SCHEDULER_ALREADY_RUNNING",
                f"project={project_name} pid={state['scheduler_pid']}",
            ))
            continue
        scheduler_process = _spawn_runtime_process(
            config,
            f"scheduler-{project_name}",
            _self_cli_command("jobs", "daemon", project_name, "--interval", str(interval)),
        )
        if not _process_survived_startup(scheduler_process):
            scheduler_failures = True
            console.print(_runtime_log_line("SCHEDULER_START_FAILED", f"project={project_name} pid={scheduler_process.pid}"))
            continue
        active_schedulers += 1
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "scheduler_pid": scheduler_process.pid,
            "project": project_name,
            "interval": interval,
            "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }, sort_keys=True), encoding="utf-8")
        console.print(_runtime_log_line(
            "SCHEDULER_STARTED",
            f"project={project_name} pid={scheduler_process.pid} interval={interval}",
        ))
    if scheduler_failures:
        if started_new_proxy and active_schedulers == 0:
            _stop_new_proxy_after_failed_start(proxy_process, proxy_state_path)
        raise typer.Exit(1)


@runtime_app.command("stop")
def runtime_stop(
    project: str | None = typer.Option(None, "--project", help="Configured project name."),
) -> None:
    """Stop the detached runtime processes."""
    config = _load_cli_config()
    projects = _resolve_projects(config, project)
    stopped_any = False
    for project_name in projects:
        state_path = _runtime_state_path(config, project_name)
        state = _load_runtime_state(state_path)
        if state is None:
            state_path.unlink(missing_ok=True)
            if project is not None:
                console.print(f"Runtime is not running for {project_name}")
            continue
        stopped_any = True
        for key in ("scheduler_pid",):
            pid = state.get(key)
            if _pid_is_running(pid):
                os.kill(int(pid), signal.SIGTERM)
                if not _wait_for_pid_exit(int(pid)):
                    console.print(_runtime_log_line(
                        "SCHEDULER_STOP_TIMEOUT",
                        f"project={project_name} pid={pid}",
                    ))
                    raise typer.Exit(1)
        _reconcile_active_runtime_jobs(
            config,
            project_name,
            reason="runtime stop requested",
            actor_id="runtime-stop",
            stop_worker_containers=True,
        )
        state_path.unlink(missing_ok=True)
    if not _any_scheduler_running(config):
        proxy_state_path = _proxy_state_path(config)
        proxy_state = _load_runtime_state(proxy_state_path)
        if proxy_state is not None and _pid_is_running(proxy_state.get("proxy_pid")):
            os.kill(int(proxy_state["proxy_pid"]), signal.SIGTERM)
            if not _wait_for_pid_exit(int(proxy_state["proxy_pid"])):
                console.print(_runtime_log_line(
                    "MODEL_PROXY_STOP_TIMEOUT",
                    f"pid={proxy_state['proxy_pid']}",
                ))
                raise typer.Exit(1)
            console.print(_runtime_log_line("MODEL_PROXY_STOPPED", f"pid={proxy_state['proxy_pid']}"))
        proxy_state_path.unlink(missing_ok=True)
    if not stopped_any:
        console.print("Runtime is not running for any configured project")
        return
    if project is not None or len(projects) == 1:
        console.print(f"Runtime stopped for {projects[0]}")
    else:
        console.print(f"Runtime stopped for {len(projects)} projects")


@runtime_app.command("status")
def runtime_status(
    project: str | None = typer.Option(None, "--project", help="Configured project name."),
) -> None:
    """Show whether the detached runtime processes are running."""
    config = _load_cli_config()
    proxy_state = _load_runtime_state(_proxy_state_path(config))
    proxy_running = proxy_state is not None and _pid_is_running(proxy_state.get("proxy_pid"))
    projects = _resolve_projects(config, project)
    lines: list[str] = []
    for project_name in projects:
        state_path = _runtime_state_path(config, project_name)
        state = _load_runtime_state(state_path)
        if state is None:
            lines.append(f"Runtime not running for {project_name}")
            continue
        scheduler_running = _pid_is_running(state.get("scheduler_pid"))
        if scheduler_running and proxy_running:
            lines.append(
                f"Runtime running for {project_name} "
                f"scheduler_pid={state['scheduler_pid']} proxy_pid={proxy_state['proxy_pid']}"
            )
            continue
        lines.append(
            f"Runtime degraded for {project_name} "
            f"scheduler_running={scheduler_running} proxy_running={proxy_running}"
        )
    for index, line in enumerate(lines):
        if index:
            console.print()
        console.print(line)


@model_proxy_app.command("serve")
def model_proxy_serve() -> None:
    """Serve configured model proxy endpoints."""
    config = _load_cli_config()
    readiness = check_backend_readiness(config.model_proxy, env=os.environ)
    for result in readiness:
        if result.ready:
            console.print(_runtime_log_line(
                "MODEL_PROXY_HEALTH_OK",
                f"proxy={result.proxy_id} status={result.status}",
            ))
        else:
            detail = f"proxy={result.proxy_id}"
            if result.status is not None:
                detail += f" status={result.status}"
            if result.error:
                detail += f" error={result.error}"
            console.print(_runtime_log_line("MODEL_PROXY_HEALTH_FAILED", detail))
    if any(not result.ready for result in readiness):
        raise typer.Exit(1)
    sessions = FileModelProxySessionStore(_model_proxy_session_root(config))
    adapters = {}
    for proxy_id, proxy in config.model_proxy.items():
        if proxy.kind == "local":
            adapters[proxy_id] = LocalModelAdapter(proxy)
        elif proxy.kind == "openai":
            adapters[proxy_id] = OpenAIAdapter(proxy, os.environ)
    transcript_root = config.model_proxy_server.log_root or (
        (config.config_dir or Path.cwd()) / "model-proxy-logs"
    )
    service = ModelProxyService(
        sessions=sessions,
        adapters=adapters,
        lease_store=FileResourceLeaseStore(
            (config.config_dir or Path.cwd()) / "resource-leases",
            config.resources,
        ),
        transcript_root=transcript_root,
        body_logging=config.model_proxy_server.body_logging,
    )
    server = serve_model_proxy(
        service,
        host=config.model_proxy_server.host,
        port=config.model_proxy_server.port,
    )
    host, port = server.server_address
    console.print(f"Serving model proxy at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


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
        repo_root=ctx["project_config"].repo_root,
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=_validation_context,
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


@transactions_app.command("list")
def list_transactions(project: str = typer.Argument(..., help="Configured project name.")) -> None:
    """List prepared and failed transaction journals."""
    ctx = _runtime_project_context(project)
    records = (
        *ctx["journal_store"].iter_journals("prepared"),
        *ctx["journal_store"].iter_journals("failed"),
    )
    if not records:
        console.print("No incomplete or failed transactions.")
        return
    for record in records:
        error = f" error={record.error.code}" if record.error is not None else ""
        console.print(
            f"{record.journal_id} status={record.status.value} task={record.task_id or '-'} "
            f"transition={record.transition_id or '-'} effects={len(record.effects)}{error}"
        )


@transactions_app.command("recover")
def recover_transactions(project: str = typer.Argument(..., help="Configured project name.")) -> None:
    """Attempt recovery of prepared job-creation and completion journals."""
    ctx = _runtime_project_context(project)
    recovered_jobs = recover_job_creation_transactions(
        job_store=ctx["job_store"],
        event_store=ctx["event_store"],
        journal_store=ctx["journal_store"],
    )
    service = CompletionService(
        workflow=ctx["workflow"],
        adapter=ctx["adapter"],
        job_store=ctx["job_store"],
        event_store=ctx["event_store"],
        journal_store=ctx["journal_store"],
        artifact_root=ctx["project_path"] / "artifacts",
        repo_root=ctx["project_config"].repo_root,
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=_validation_context,
    )
    recovered_completions = recover_completion_transactions(
        service=service,
        event_store=ctx["event_store"],
        journal_store=ctx["journal_store"],
    )
    console.print(
        f"Recovered job journals={len(recovered_jobs)} completion journals={len(recovered_completions)}."
    )


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
        repo_root=ctx["project_config"].repo_root,
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=_validation_context,
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
    project: str | None = typer.Argument(None, help="Configured project name."),
) -> None:
    """List tasks from one project or all configured projects."""
    config = _load_cli_config()
    projects = _resolve_projects(config, project)
    for index, project_name in enumerate(projects):
        ctx = _runtime_project_context(project_name)
        loaded = ctx["adapter"].load_project()
        if not loaded.accepted or loaded.snapshot is None:
            _print_domain_errors(loaded.errors)
            raise typer.Exit(1)
        if index:
            console.print()
        console.print(f"{project_name}:")
        if not loaded.snapshot.tasks:
            console.print("  No tasks.")
            continue
        for task in sorted(loaded.snapshot.tasks.values(), key=lambda item: item.id):
            console.print(
                f"  {task.id}  state={task.current_state}  type={task.task_type}  title={task.title}",
            )


@tasks_app.command("runnable")
def runnable_tasks(
    project: str | None = typer.Argument(None, help="Configured project name."),
) -> None:
    """List tasks with worker-backed outgoing transitions."""
    config = _load_cli_config()
    projects = _resolve_projects(config, project)
    found = False
    for project_name in projects:
        ctx = _runtime_project_context(project_name)
        loaded = ctx["adapter"].load_project()
        if not loaded.accepted or loaded.snapshot is None:
            _print_domain_errors(loaded.errors)
            raise typer.Exit(1)
        lines: list[str] = []
        for task in sorted(loaded.snapshot.tasks.values(), key=lambda item: item.id):
            transitions = [
                transition for transition in ctx["workflow"].transitions.values()
                if transition.task_type == task.task_type
                and transition.from_state == task.current_state
                and transition.worker is not None
            ]
            for transition in transitions:
                lines.append(f"  {task.id}  transition={transition.id}  worker={transition.worker}")
        if not lines:
            continue
        if found:
            console.print()
        found = True
        console.print(f"{project_name}:")
        for line in lines:
            console.print(line)
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

    def apply_effect(effect):
        if effect.get("type") != "move_task":
            return OperationResult(False, "effect.unknown", f"Unknown effect: {effect.get('type')}")
        moved = ctx["adapter"].move_task(str(effect["task_id"]), str(effect["to_state"]))
        return OperationResult(moved.accepted, "move_task", errors=moved.errors)

    def compensate_effect(effect):
        if effect.get("type") == "move_task":
            moved = ctx["adapter"].move_task(str(effect["task_id"]), str(effect["from_state"]))
            return OperationResult(moved.accepted, "move_task", errors=moved.errors)
        return OperationResult(True, "noop")

    def validate_final_state():
        loaded = ctx["adapter"].read_task(task_id)
        if not loaded.accepted or loaded.task is None:
            return OperationResult(False, "read_task", errors=loaded.errors)
        accepted = loaded.task.current_state == transition.to_state
        return OperationResult(accepted, "validate_final_state", (
            "" if accepted else f"Task ended in {loaded.task.current_state}, expected {transition.to_state}."
        ))

    applied = FileTransactionRuntime(
        journals=ctx["journal_store"],
        events=ctx["event_store"],
        apply_effect=apply_effect,
        compensate_effect=compensate_effect,
        validate_final_state=validate_final_state,
    ).apply(
        project_id=project,
        task_id=task_id,
        transition_id=transition_id,
        effects=checked.effects,
        events=checked.events,
    )
    if not applied.accepted:
        _print_domain_errors((applied.error,))
        raise typer.Exit(1)
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
    config, workflow = _load_cli_context(project)
    if workflow is None:
        console.print(Panel(f"workflow.yaml is required in project {project!r}.", style="red"))
        raise typer.Exit(2)
    project_path = _project_path(config, project)
    project_config = config.project_configs.get(project) or ProjectConfig(
        name=project,
        tracker_path=project,
    )
    adapter = build_storage_adapter(AdapterBuildRequest(
        project_id=project,
        project_root=project_path,
        tracker_type=config.tracker_type,
        workflow=workflow,
    ))
    app_state = config.config_dir or (Path.home() / CONFIG_DIRNAME)
    job_store = FileExecutionJobStore(app_state / "jobs" / project)
    event_store = JsonlEventStore(project_path / "events")
    journal_store = TransactionJournalStore(project_path / "events" / "journals")
    recovered = recover_job_creation_transactions(
        job_store=job_store,
        event_store=event_store,
        journal_store=journal_store,
    )
    for journal_id in recovered:
        console.print(_runtime_log_line(
            "JOB_CREATION_RECOVERED",
            f"project={project} journal={journal_id}",
        ))
    completion_recovery_service = CompletionService(
        workflow=workflow,
        adapter=adapter,
        job_store=job_store,
        event_store=event_store,
        journal_store=journal_store,
        artifact_root=project_path / "artifacts",
        repo_root=project_config.repo_root,
        validation_implementations=VALIDATION_IMPLEMENTATIONS,
        validation_context_factory=_validation_context,
    )
    recovered_completions = recover_completion_transactions(
        service=completion_recovery_service,
        event_store=event_store,
        journal_store=journal_store,
    )
    for journal_id in recovered_completions:
        console.print(_runtime_log_line(
            "COMPLETION_RECOVERED",
            f"project={project} journal={journal_id}",
        ))
    lease_store = FileResourceLeaseStore(
        app_state / "resource-leases",
        config.resources,
    )
    listed_jobs = job_store.list()
    if listed_jobs.accepted:
        active_statuses = {
            ExecutionJobStatus.PENDING.value,
            ExecutionJobStatus.RUNNING.value,
            ExecutionJobStatus.COMPLETION_REJECTED.value,
            ExecutionJobStatus.STALE.value,
        }
        active_job_ids = {
            job.job_id
            for job in listed_jobs.jobs
            if str(job.status.value if hasattr(job.status, "value") else job.status) in active_statuses
        }
        released = lease_store.release_inactive_reservations(active_job_ids)
        for job_id in released:
            console.print(_runtime_log_line(
                "RESOURCE_LEASE_RELEASED",
                f"project={project} job={job_id} reason=inactive_job",
            ))
    return {
        "config": config,
        "workflow": workflow,
        "project_config": project_config,
        "adapter": adapter,
        "job_store": job_store,
        "event_store": event_store,
        "journal_store": journal_store,
        "project_path": project_path,
        "workspace_root": default_shared_workspace_root(config.runtime, app_state),
        "lease_store": lease_store,
        "model_proxy_sessions": FileModelProxySessionStore(_model_proxy_session_root(config)),
    }


def _print_domain_errors(errors) -> None:
    for error in errors:
        if error is None:
            continue
        location = f" [{error.location}]" if error.location else ""
        console.print(f"[red]{error.code}[/red]{location}: {error.message}")


def _model_proxy_session_root(config: Config) -> Path:
    return (config.config_dir or Path.cwd()) / "model-proxy-sessions"


def _model_proxy_endpoint_base(config: Config) -> str:
    return f"http://{config.runtime.completion_container_host}:{config.model_proxy_server.port}"


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
    if status in {
        ExecutionJobStatus.ACCEPTED,
        ExecutionJobStatus.FAILED,
        ExecutionJobStatus.STALE,
        ExecutionJobStatus.CANCELLED,
    }:
        ctx["lease_store"].release_job(job_id)
        ctx["model_proxy_sessions"].revoke_job(job_id)
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


def _resolve_projects(config: Config, project: str | None) -> list[str]:
    if project is not None:
        _project_path(config, project)
        return [project]
    return list(config.projects)


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


def _runtime_state_path(config: Config, project: str) -> Path:
    return (config.config_dir or Path.home() / CONFIG_DIRNAME) / "runtime" / f"{project}.json"


def _proxy_state_path(config: Config) -> Path:
    return (config.config_dir or Path.cwd()) / "model-proxy-runtime.json"


def _load_runtime_state(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _runtime_log_line(event_type: str, detail: str) -> str:
    timestamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return f">>> {timestamp} {event_type} {detail}"


def _runtime_log_root(config: Config) -> Path:
    return (config.config_dir or Path.cwd()) / "runtime-logs"


def _spawn_runtime_process(config: Config, name: str, args: list[str]) -> subprocess.Popen:
    log_root = _runtime_log_root(config)
    log_root.mkdir(parents=True, exist_ok=True)
    stdout = (log_root / f"{name}.stdout.log").open("ab")
    stderr = (log_root / f"{name}.stderr.log").open("ab")
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )


def _self_cli_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "open_tulid", *args]


def _process_survived_startup(process: subprocess.Popen, *, delay: float = 0.1) -> bool:
    time.sleep(delay)
    poll = getattr(process, "poll", None)
    return poll is None or poll() is None


def _stop_new_proxy_after_failed_start(process: subprocess.Popen, state_path: Path) -> None:
    if _pid_is_running(process.pid):
        os.kill(process.pid, signal.SIGTERM)
    state_path.unlink(missing_ok=True)
    console.print(_runtime_log_line("MODEL_PROXY_STOPPED_AFTER_START_FAILURE", f"pid={process.pid}"))


def _proxy_listener_ready(
    config: Config,
    *,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> bool:
    host = config.model_proxy_server.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, config.model_proxy_server.port), timeout=poll_interval):
                return True
        except OSError:
            time.sleep(poll_interval)
    return False


def _any_scheduler_running(config: Config) -> bool:
    for project in config.projects:
        state = _load_runtime_state(_runtime_state_path(config, project))
        if state is not None and _pid_is_running(state.get("scheduler_pid")):
            return True
    return False


def _fail_orphaned_runtime_jobs(config: Config, project: str) -> None:
    _reconcile_active_runtime_jobs(
        config,
        project,
        reason="orphaned after detached scheduler was not running at runtime start",
        actor_id="runtime-start",
        stop_worker_containers=False,
    )


def _reconcile_active_runtime_jobs(
    config: Config,
    project: str,
    *,
    reason: str,
    actor_id: str,
    stop_worker_containers: bool,
) -> None:
    app_state = config.config_dir or (Path.home() / CONFIG_DIRNAME)
    job_store = FileExecutionJobStore(app_state / "jobs" / project)
    event_store = JsonlEventStore(_project_path(config, project) / "events")
    lease_store = FileResourceLeaseStore(app_state / "resource-leases", config.resources)
    model_proxy_sessions = FileModelProxySessionStore(_model_proxy_session_root(config))
    listed = job_store.list()
    if not listed.accepted:
        return
    orphanable = {
        ExecutionJobStatus.PENDING.value,
        ExecutionJobStatus.RUNNING.value,
        ExecutionJobStatus.COMPLETION_REJECTED.value,
    }
    for job in listed.jobs:
        status = job.status.value if hasattr(job.status, "value") else str(job.status)
        if status not in orphanable:
            continue
        if stop_worker_containers:
            _stop_worker_container(config.runtime.docker_executable, job.job_id)
        updated = job_store.update_status(
            job.job_id,
            ExecutionJobStatus.FAILED,
            metadata={"status_reason": reason},
        )
        if not updated.accepted:
            continue
        lease_store.release_job(job.job_id)
        model_proxy_sessions.revoke_job(job.job_id)
        event_store.append(build_event(
            project_id=job.project_id,
            actor=EventActor(type="system", id=actor_id),
            event_type=EventType.ExecutionFailed,
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            data={"reason": reason.replace(" ", "_")},
        ))
        console.print(_runtime_log_line(
            "JOB_ORPHANED",
            f"project={project} job={job.job_id} prior_status={status}",
        ))


def _stop_worker_container(docker_executable: str, job_id: str) -> None:
    name = _worker_container_name(job_id)
    try:
        result = subprocess.run(
            (docker_executable, "rm", "-f", name),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.print(_runtime_log_line(
            "WORKER_CONTAINER_STOP_FAILED",
            f"job={job_id} name={name} error={exc}",
        ))
        return
    if result.returncode == 0:
        console.print(_runtime_log_line(
            "WORKER_CONTAINER_STOPPED",
            f"job={job_id} name={name}",
        ))
        return
    stderr = (result.stderr or "").strip().lower()
    if "no such container" in stderr:
        return
    console.print(_runtime_log_line(
        "WORKER_CONTAINER_STOP_FAILED",
        f"job={job_id} name={name} exit_code={result.returncode}",
    ))


def _worker_container_name(job_id: str) -> str:
    return f"open-tulid-job-{job_id.lower()}"


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


def _wait_for_pid_exit(pid: int, *, timeout: float = 5.0, poll_interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(poll_interval)
    return not _pid_is_running(pid)


def _validation_context(workspace: Path, output_root: Path) -> WorkflowExecutionContext:
    return WorkflowExecutionContext(
        project_root=workspace,
        vault_root=output_root,
    )
