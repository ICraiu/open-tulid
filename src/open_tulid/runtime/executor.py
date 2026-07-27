from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Mapping

from open_tulid.adapters.base import StorageAdapter
from open_tulid.containers import AgentRunResult, ContainerMount, ContainersService, build_containers_service
from open_tulid.domain import (
    DomainError,
    EventActor,
    EventType,
    ExecutionJobStatus,
    Task,
    TransitionDefinition,
    ValidationCallDefinition,
    WorkflowDefinition,
)
from open_tulid.models import ModelProxyConfig, ProjectConfig, ResourceConfig, RuntimeConfig
from open_tulid.runtime.completion import CompletionService
from open_tulid.runtime.completion_http import CompletionEndpointConfig, serve_completion_endpoint
from open_tulid.runtime.events import JsonlEventStore, TransactionJournalStore, build_event
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.instructions import AgentInstructionResolver, PromptPacket
from open_tulid.runtime.context import LinkedContextResolver, sanitize_task_body_for_runtime
from open_tulid.runtime.resources import FileResourceLeaseStore
from open_tulid.runtime.model_proxy import FileModelProxySessionStore, ModelProxySessionStore
from open_tulid.runtime.workspaces import WorkspacePreparer

TERMINAL_JOB_STATUSES = frozenset({
    ExecutionJobStatus.ACCEPTED.value,
    ExecutionJobStatus.FAILED.value,
    ExecutionJobStatus.STALE.value,
    ExecutionJobStatus.CANCELLED.value,
})

COMPLETION_SETTLE_STATUSES = frozenset({
    ExecutionJobStatus.COMPLETION_SUBMITTED.value,
})

DEFAULT_COMPLETION_SETTLE_TIMEOUT_SECONDS = 1800.0
OPENCODE_TULID_AGENT = "tulid-build"
_active_containers_service: ContainersService | None = None


@dataclass(frozen=True)
class ExecutorRunResult:
    accepted: bool
    run: AgentRunResult | None = None
    errors: tuple[DomainError, ...] = ()


@dataclass(frozen=True)
class PromptRenderResult:
    text: str = ""
    instruction_packet: PromptPacket | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


def render_execution_prompt(
    *,
    workflow: WorkflowDefinition,
    adapter: StorageAdapter,
    task: Task,
    transition: TransitionDefinition,
    worker_id: str,
    job_id: str,
    completion_endpoint: str,
) -> PromptRenderResult:
    """Render the exact model prompt for an execution job without running it."""
    worker = workflow.workers.get(worker_id)
    task_type = workflow.task_types.get(transition.task_type)
    prompt_text = _build_runtime_prompt(
        job_id=job_id,
        task_title=task.title,
        task_body=sanitize_task_body_for_runtime(task.body),
        transition_id=transition.id,
        from_state=transition.from_state,
        to_state=transition.to_state,
        required_artifacts=transition.requires.artifacts,
        required_validations=_validation_ids(transition.requires.validations),
        required_validation_details=_validation_details(transition.requires.validations),
        changed_files_required=transition.requires.changed_files_required,
        derived_artifact_type=transition.derives.artifact_type if transition.derives is not None else None,
        completion_endpoint=completion_endpoint,
    )
    prompt_packet = None
    project_root = _adapter_project_root(adapter)
    if project_root is not None:
        parent_tasks = _load_parent_tasks(adapter, task)
        task_for_context = _task_for_prompt_context(task, transition)
        prompt_text = _append_parent_tasks(prompt_text, parent_tasks)
        context_result = LinkedContextResolver(project_root).build_context_packet(
            task_for_context,
            parent_tasks=parent_tasks,
        )
        if not context_result.accepted:
            return PromptRenderResult(errors=context_result.errors)
        context_packet = context_result.packet
        if context_packet is not None and context_packet.text:
            prompt_text = f"{prompt_text}\n\n{context_packet.text}"
        prompt_result = AgentInstructionResolver(project_root).build_prompt_packet(
            worker=worker,
            task_type=task_type,
            transition=transition,
        )
        if not prompt_result.accepted:
            return PromptRenderResult(errors=prompt_result.errors or (_error(
                "instructions.invalid",
                "Prompt instructions failed.",
            ),))
        prompt_packet = prompt_result.packet
        if prompt_packet is not None:
            prompt_text = f"{prompt_text}\n\n{prompt_packet.text}"
    prompt_text = _append_final_completion_reminder(
        prompt_text,
        required_artifacts=transition.requires.artifacts,
        required_validations=_validation_ids(transition.requires.validations),
        required_validation_details=_validation_details(transition.requires.validations),
        changed_files_required=transition.requires.changed_files_required,
    )
    return PromptRenderResult(
        text=prompt_text,
        instruction_packet=prompt_packet,
    )


class JobExecutor:
    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        adapter: StorageAdapter,
        job_store: FileExecutionJobStore,
        event_store: JsonlEventStore,
        runtime: RuntimeConfig,
        project_config: ProjectConfig,
        journal_store: TransactionJournalStore | None = None,
        artifact_root: Path | None = None,
        lease_store: FileResourceLeaseStore | None = None,
        resources: dict[str, ResourceConfig] | None = None,
        model_proxies: dict[str, ModelProxyConfig] | None = None,
        model_proxy_sessions: ModelProxySessionStore | FileModelProxySessionStore | None = None,
        model_proxy_endpoint_base: str | None = None,
        validation_implementations: Mapping[str, object] | None = None,
        validation_context_factory: object | None = None,
        completion_settle_timeout_seconds: float = DEFAULT_COMPLETION_SETTLE_TIMEOUT_SECONDS,
        containers: ContainersService | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.event_store = event_store
        self.runtime = runtime
        self.project_config = project_config
        self.journal_store = journal_store
        self.artifact_root = artifact_root
        self.lease_store = lease_store
        self.resources = resources or {}
        self.model_proxies = model_proxies or {}
        self.model_proxy_sessions = model_proxy_sessions
        self.model_proxy_endpoint_base = model_proxy_endpoint_base
        self.validation_implementations = validation_implementations
        self.validation_context_factory = validation_context_factory
        self.completion_settle_timeout_seconds = completion_settle_timeout_seconds
        self.containers = containers or build_containers_service()

    def run(self, job_id: str) -> ExecutorRunResult:
        loaded = self.job_store.get(job_id)
        if not loaded.accepted or loaded.job is None:
            return ExecutorRunResult(False, errors=(loaded.error or _error("job.not_found", "Job was not found."),))
        job = loaded.job
        job_status = job.status.value if isinstance(job.status, ExecutionJobStatus) else str(job.status)
        if job_status in TERMINAL_JOB_STATUSES:
            return ExecutorRunResult(False, errors=(_error(
                "job.terminal",
                f"Execution job {job.job_id!r} is terminal: {job_status}.",
                job.job_id,
            ),))
        transition = self.workflow.transitions.get(job.transition_id)
        if transition is None:
            return ExecutorRunResult(False, errors=(_error("transition.not_found", "Transition was not found."),))
        worker = self.workflow.workers.get(job.worker_id)
        task_result = self.adapter.read_task(job.task_id)
        if not task_result.accepted or task_result.task is None:
            return ExecutorRunResult(False, errors=task_result.errors or (_error("task.not_found", "Task was not found."),))

        required_resources = self.runtime.worker_resources.get(job.worker_id, ())
        lease_acquired = False
        if required_resources and self.lease_store is not None:
            if not self.lease_store.job_holds(required_resources, job.job_id):
                lease_result = self.lease_store.try_acquire(
                    required_resources,
                    job_id=job.job_id,
                    worker_id=job.worker_id,
                    owner_path=self.job_store.path_for(job.job_id),
                )
                if not lease_result.acquired:
                    return ExecutorRunResult(False, errors=(_error(
                        "resource.busy",
                        f"Execution job {job.job_id!r} requires busy resources: "
                        f"{', '.join(lease_result.busy_resources)}.",
                        job.job_id,
                    ),))
            lease_acquired = True

        try:
            endpoint = self._start_completion_endpoint(job.job_id)
            prepared = WorkspacePreparer(repo_root=self.project_config.repo_root).prepare(
                job=job,
                task=task_result.task,
                transition=transition,
                completion_endpoint=endpoint.url,
            )
            if not prepared.accepted or prepared.workspace is None:
                endpoint.stop()
                return self._fail_before_run(
                    job,
                    prepared.error or _error("workspace.prepare_failed", "Workspace failed."),
                )

            rendered_prompt = render_execution_prompt(
                workflow=self.workflow,
                adapter=self.adapter,
                task=task_result.task,
                transition=transition,
                worker_id=job.worker_id,
                job_id=job.job_id,
                completion_endpoint=endpoint.url,
            )
            if not rendered_prompt.accepted:
                endpoint.stop()
                return self._fail_before_run(job, rendered_prompt.errors[0])
            prompt_text = rendered_prompt.text
            prompt_packet = rendered_prompt.instruction_packet
            prompt_sha256 = _write_prompt_packet(prepared.workspace, prompt_text)

            self.job_store.update_status(
                job.job_id,
                ExecutionJobStatus.RUNNING,
                metadata={
                    "workspace_prepared": True,
                    "completion_endpoint": endpoint.url,
                    "completion_endpoint_host": endpoint.host,
                    "completion_endpoint_port": endpoint.port,
                    "prompt_packet_sha256": prompt_sha256,
                    "instruction_packet_sha256": prompt_packet.sha256 if prompt_packet is not None else None,
                },
                increment_attempts=True,
            )
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="executor"),
                event_type=EventType.ExecutionStarted,
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                data={"worker_id": job.worker_id, "workspace_path": str(prepared.workspace)},
            ))

            implementation_id = _execution_worker_id(worker, job.worker_id)
            model_proxy_env = self._model_proxy_env(job.job_id, job.worker_id, required_resources)
            worker_args = _worker_args(
                runtime=self.runtime,
                worker_id=job.worker_id,
                implementation_id=implementation_id,
                container_workspace=self.runtime.container_workspace,
                completion_endpoint=endpoint.url,
            )
            _write_opencode_model_config_if_needed(
                workspace=prepared.workspace,
                runtime=self.runtime,
                worker_id=job.worker_id,
                implementation_id=implementation_id,
                args=worker_args,
                env=model_proxy_env,
            )

            request = self.containers.request_for_worker(
                worker_id=_execution_worker_id(worker, job.worker_id),
                workspace=prepared.workspace,
                runtime=self.runtime,
                args=worker_args,
                env={
                    "OPEN_TULID_JOB_ID": job.job_id,
                    "OPEN_TULID_COMPLETION_TOKEN": str(job.metadata.get("completion_token", "")),
                    "OPEN_TULID_OUTPUT_DIR": f"{self.runtime.container_workspace}/output",
                    "OPEN_TULID_COMPLETION_ENDPOINT": endpoint.url,
                    "OPEN_TULID_PROMPT_PACKET": f"{self.runtime.container_workspace}/.open-tulid/prompt-packet.md",
                    **model_proxy_env,
                },
                mounts=self._subscription_mounts(required_resources),
            )
            log_dir = _agent_log_dir(prepared.workspace)
            started_at = _utc_now()
            _write_run_trace(
                log_dir,
                job=job,
                request=request,
                status="running",
                started_at=started_at,
            )
            result = None
            try:
                result = _run_agent_container_with_logs(
                    self.containers,
                    request,
                    docker_executable=self.runtime.docker_executable,
                    log_dir=log_dir,
                )
                if result.succeeded:
                    self._wait_for_completion_settlement(job.job_id)
            finally:
                endpoint.stop()
            assert result is not None
            finished_at = _utc_now()
            _write_run_logs_with_metadata(
                Path(job.workspace_path),
                result,
                request=request,
                job=job,
                started_at=started_at,
                finished_at=finished_at,
            )
            loaded_after_run = self.job_store.get(job.job_id)
            status_after_run = (
                str(
                    loaded_after_run.job.status.value
                    if hasattr(loaded_after_run.job.status, "value")
                    else loaded_after_run.job.status
                )
                if loaded_after_run.accepted and loaded_after_run.job is not None
                else ""
            )
            if status_after_run == ExecutionJobStatus.ACCEPTED.value:
                return ExecutorRunResult(True, run=result)
            if status_after_run in {
                ExecutionJobStatus.FAILED.value,
                ExecutionJobStatus.STALE.value,
                ExecutionJobStatus.CANCELLED.value,
            }:
                return ExecutorRunResult(True, run=result)
            if not result.succeeded:
                self.job_store.update_status(
                    job.job_id,
                    ExecutionJobStatus.FAILED,
                    metadata={"worker_returncode": result.returncode},
                )
                self.event_store.append(build_event(
                    project_id=job.project_id,
                    actor=EventActor(type="system", id="executor"),
                    event_type=EventType.ExecutionFailed,
                    correlation_id=job.job_id,
                    task_id=job.task_id,
                    job_id=job.job_id,
                    transition_id=job.transition_id,
                    data={"returncode": result.returncode},
                ))
            else:
                self.job_store.update_status(
                    job.job_id,
                    ExecutionJobStatus.FAILED,
                    metadata={"worker_returncode": result.returncode, "failure_reason": "completion_not_accepted"},
                )
                self.event_store.append(build_event(
                    project_id=job.project_id,
                    actor=EventActor(type="system", id="executor"),
                    event_type=EventType.ExecutionFailed,
                    correlation_id=job.job_id,
                    task_id=job.task_id,
                    job_id=job.job_id,
                    transition_id=job.transition_id,
                    data={"returncode": result.returncode, "reason": "completion_not_accepted"},
                ))
            return ExecutorRunResult(True, run=result)
        except Exception as exc:
            self.job_store.update_status(
                job.job_id,
                ExecutionJobStatus.FAILED,
                metadata={"failure_reason": "executor_exception", "failure_detail": str(exc)},
            )
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="executor"),
                event_type=EventType.ExecutionFailed,
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                data={"reason": "executor_exception", "detail": str(exc)},
            ))
            return ExecutorRunResult(False, errors=(_error(
                "executor.exception",
                f"Execution job {job.job_id!r} failed unexpectedly: {exc}",
                job.job_id,
            ),))
        finally:
            if lease_acquired and self.lease_store is not None:
                self.lease_store.release_job(job.job_id)
            if self.model_proxy_sessions is not None:
                self.model_proxy_sessions.revoke_job(job.job_id)

    def _wait_for_completion_settlement(self, job_id: str) -> None:
        deadline = time.monotonic() + max(0.0, self.completion_settle_timeout_seconds)
        while True:
            loaded = self.job_store.get(job_id)
            status = (
                str(
                    loaded.job.status.value
                    if hasattr(loaded.job.status, "value")
                    else loaded.job.status
                )
                if loaded.accepted and loaded.job is not None
                else ""
            )
            if status not in COMPLETION_SETTLE_STATUSES:
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.25)

    def _start_completion_endpoint(self, job_id: str) -> "_ManagedCompletionEndpoint":
        service = CompletionService(
            workflow=self.workflow,
            adapter=self.adapter,
            job_store=self.job_store,
            event_store=self.event_store,
            journal_store=self.journal_store,
            artifact_root=self.artifact_root,
            repo_root=self.project_config.repo_root,
            validation_implementations=self.validation_implementations,
            validation_context_factory=self.validation_context_factory,
        )
        server = serve_completion_endpoint(
            CompletionEndpointConfig(
                service=service,
                allowed_jobs=frozenset({job_id}),
            ),
            host=self.runtime.completion_host,
            port=self.runtime.completion_port,
        )
        host, port = server.server_address
        thread = Thread(target=server.serve_forever, name=f"open-tulid-completion-{job_id}", daemon=True)
        thread.start()
        return _ManagedCompletionEndpoint(
            server=server,
            thread=thread,
            host=str(host),
            port=int(port),
            url=f"http://{self.runtime.completion_container_host}:{port}/jobs/{job_id}/complete",
        )

    def _fail_before_run(self, job, error: DomainError) -> ExecutorRunResult:
        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.FAILED,
            metadata={"failure_reason": error.code, "failure_detail": error.message},
        )
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=EventActor(type="system", id="executor"),
            event_type=EventType.ExecutionFailed,
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            data={"reason": error.code, "detail": error.message},
        ))
        return ExecutorRunResult(False, errors=(error,))

    def _model_proxy_env(
        self,
        job_id: str,
        worker_id: str,
        required_resources: tuple[str, ...],
    ) -> dict[str, str]:
        if self.model_proxy_sessions is None or self.model_proxy_endpoint_base is None:
            return {}
        endpoints: list[dict[str, str]] = []
        for resource_id in required_resources:
            resource = self.resources.get(resource_id)
            if resource is None or resource.proxy is None:
                continue
            proxy = self.model_proxies.get(resource.proxy)
            if proxy is not None and proxy.kind == "subscription":
                continue
            session = self.model_proxy_sessions.issue(
                job_id=job_id,
                worker_id=worker_id,
                proxy_id=resource.proxy,
                resource_id=resource_id,
            )
            endpoints.append({
                "resource_id": resource_id,
                "endpoint": f"{self.model_proxy_endpoint_base.rstrip('/')}/proxies/{resource.proxy}",
                "token": session.token,
                "proxy_id": resource.proxy,
            })
        if not endpoints:
            return {}
        env = {"OPEN_TULID_MODEL_ENDPOINTS": json.dumps(endpoints, sort_keys=True)}
        if len(endpoints) == 1:
            endpoint = endpoints[0]
            env.update({
                "OPEN_TULID_MODEL_ENDPOINT": endpoint["endpoint"],
                "OPEN_TULID_MODEL_SESSION_TOKEN": endpoint["token"],
                "OPEN_TULID_MODEL_PROXY_ID": endpoint["proxy_id"],
            })
            env.update(_render_worker_model_env(
                self.runtime.worker_model_env.get(worker_id, {}),
                endpoint,
            ))
        return env

    def _subscription_mounts(self, required_resources: tuple[str, ...]) -> tuple[ContainerMount, ...]:
        mounts: list[ContainerMount] = []
        for resource_id in required_resources:
            resource = self.resources.get(resource_id)
            if resource is None or resource.proxy is None:
                continue
            proxy = self.model_proxies.get(resource.proxy)
            if proxy is None or proxy.kind != "subscription":
                continue
            assert proxy.auth_home is not None
            assert proxy.container_auth_home is not None
            mounts.append(ContainerMount(proxy.auth_home, proxy.container_auth_home))
        return tuple(mounts)


@dataclass(frozen=True)
class _ManagedCompletionEndpoint:
    server: object
    thread: Thread
    host: str
    port: int
    url: str

    def stop(self) -> None:
        shutdown = getattr(self.server, "shutdown")
        server_close = getattr(self.server, "server_close")
        shutdown()
        server_close()
        self.thread.join(timeout=5)


def _agent_log_dir(workspace: Path) -> Path:
    return workspace / ".open-tulid" / "logs"


def _run_agent_container_with_logs(
    containers: ContainersService,
    request,
    *,
    docker_executable: str,
    log_dir: Path,
) -> AgentRunResult:
    global _active_containers_service
    previous = _active_containers_service
    _active_containers_service = containers
    try:
        return run_agent_container(request, docker_executable=docker_executable, log_dir=log_dir)
    except TypeError as exc:
        if "log_dir" not in str(exc):
            raise
        return run_agent_container(request, docker_executable=docker_executable)
    finally:
        _active_containers_service = previous


def run_agent_container(
    request,
    *,
    docker_executable: str,
    log_dir: Path | None = None,
) -> AgentRunResult:
    service = _active_containers_service or build_containers_service()
    return service.run_agent_container(
        request,
        docker_executable=docker_executable,
        log_dir=log_dir,
    )


def _write_run_logs_with_metadata(
    workspace: Path,
    result: AgentRunResult,
    *,
    request,
    job,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    try:
        _write_run_logs(
            workspace,
            result,
            request=request,
            job=job,
            started_at=started_at,
            finished_at=finished_at,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        _write_run_logs(workspace, result)


def _write_run_logs(
    workspace: Path,
    result: AgentRunResult,
    *,
    request=None,
    job=None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    log_dir = _agent_log_dir(workspace)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (log_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    (log_dir / "agent.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    (log_dir / "command.txt").write_text(" ".join(_redact_command_for_log(result.command)) + "\n", encoding="utf-8")
    _write_run_trace(
        log_dir,
        job=job,
        request=request,
        result=result,
        status="finished",
        started_at=started_at,
        finished_at=finished_at,
    )


def _write_run_trace(
    log_dir: Path,
    *,
    status: str,
    job=None,
    request=None,
    result: AgentRunResult | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    trace: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "started_at": _format_time(started_at),
        "finished_at": _format_time(finished_at),
    }
    if started_at is not None and finished_at is not None:
        trace["duration_seconds"] = round((finished_at - started_at).total_seconds(), 3)
    if job is not None:
        trace["job"] = {
            "job_id": job.job_id,
            "project_id": job.project_id,
            "task_id": job.task_id,
            "transition_id": job.transition_id,
            "worker_id": job.worker_id,
        }
    if request is not None:
        trace["agent"] = {
            "agent_id": request.agent_id,
            "image": request.image,
            "args": list(request.args),
            "workdir": request.workdir,
            "container_name": request.container_name,
            "timeout_seconds": request.timeout_seconds,
            "env": _redact_env_for_log(dict(request.env)),
            "mounts": [
                {
                    "host_path": str(mount.host_path),
                    "container_path": mount.container_path,
                    "readonly": mount.readonly,
                }
                for mount in request.mounts
            ],
            "extra_hosts": list(request.extra_hosts),
        }
    if result is not None:
        trace["result"] = {
            "returncode": result.returncode,
            "succeeded": result.succeeded,
            "command": list(_redact_command_for_log(result.command)),
            "agent_log": "agent.log",
            "stdout_log": "stdout.log",
            "stderr_log": "stderr.log",
            "command_log": "command.txt",
        }
    (log_dir / "agent-run.json").write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _render_worker_model_env(
    templates: Mapping[str, str],
    endpoint: Mapping[str, str],
) -> dict[str, str]:
    values = {
        "endpoint": endpoint["endpoint"],
        "token": endpoint["token"],
        "proxy_id": endpoint["proxy_id"],
        "resource_id": endpoint["resource_id"],
    }
    return {key: value.format_map(values) for key, value in templates.items()}


_SCOPED_TOKEN_ENV_KEYS = frozenset({
    "OPEN_TULID_COMPLETION_TOKEN",
    "OPEN_TULID_MODEL_SESSION_TOKEN",
    "OPEN_TULID_MODEL_ENDPOINTS",
})


def _redact_command_for_log(command: tuple[str, ...]) -> tuple[str, ...]:
    redacted: list[str] = []
    for part in command:
        key, separator, _value = part.partition("=")
        if separator and _should_redact_env_key(key):
            redacted.append(f"{key}=<redacted>")
        else:
            redacted.append(part)
    return tuple(redacted)


def _redact_env_for_log(env: Mapping[str, str]) -> dict[str, str]:
    return {
        key: "<redacted>" if _should_redact_env_key(key) else value
        for key, value in sorted(env.items())
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _should_redact_env_key(key: str) -> bool:
    normalized = key.upper()
    return (
        key in _SCOPED_TOKEN_ENV_KEYS
        or "TOKEN" in normalized
        or "SECRET" in normalized
        or normalized.endswith("_KEY")
    )


def _write_prompt_packet(workspace: Path, text: str) -> str:
    context_dir = workspace / ".open-tulid"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "prompt-packet.md").write_text(text + "\n", encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validation_ids(validations: tuple[ValidationCallDefinition, ...]) -> tuple[str, ...]:
    return tuple(call.type for call in validations)


def _validation_details(validations: tuple[ValidationCallDefinition, ...]) -> tuple[str, ...]:
    details: list[str] = []
    for call in validations:
        command = call.args.get("command")
        if isinstance(command, str) and command.strip():
            details.append(f"{call.type}: run `{command.strip()}`")
        elif call.args:
            args = json.dumps(dict(call.args), sort_keys=True)
            details.append(f"{call.type}: args `{args}`")
        else:
            details.append(call.type)
    return tuple(details)


def _build_runtime_prompt(
    *,
    job_id: str,
    task_title: str,
    task_body: str,
    transition_id: str,
    from_state: str,
    to_state: str,
    required_artifacts: tuple[str, ...],
    required_validations: tuple[str, ...],
    required_validation_details: tuple[str, ...],
    changed_files_required: bool,
    derived_artifact_type: str | None,
    completion_endpoint: str,
) -> str:
    variant = _runtime_prompt_variant(
        transition_id=transition_id,
        required_artifacts=required_artifacts,
        derived_artifact_type=derived_artifact_type,
    )
    artifacts = ", ".join(required_artifacts) if required_artifacts else "none"
    validations = ", ".join(required_validations) if required_validations else "none"
    sections: list[str] = [
        "# Open Tulid Job",
        "",
        f"Job: {job_id}",
        f"Task: {task_title}",
        f"Transition: {transition_id} ({from_state} -> {to_state})",
        "",
        _render_prompt_role_section(variant),
        "",
        _render_prompt_primary_objective_section(variant),
        "",
        _render_prompt_priority_section(variant),
        "",
        _render_prompt_paths_section(variant, required_artifacts=required_artifacts),
        "",
        _render_validation_failure_policy_section(variant),
        "",
        _render_prompt_completion_section(
            completion_endpoint=completion_endpoint,
            required_artifacts=required_artifacts,
            required_validations=required_validations,
            required_validation_details=required_validation_details,
            changed_files_required=changed_files_required,
            derived_artifact_type=derived_artifact_type,
            artifacts=artifacts,
            validations=validations,
        ),
        "",
        "## Task Body",
        task_body.strip(),
    ]
    return "\n".join(sections)


def _append_final_completion_reminder(
    prompt_text: str,
    *,
    required_artifacts: tuple[str, ...],
    required_validations: tuple[str, ...],
    required_validation_details: tuple[str, ...],
    changed_files_required: bool,
) -> str:
    validations = ", ".join(required_validations) if required_validations else "none"
    lines = [
        "## Final Required Step",
        "ULTRA IMPORTANT: do not stop after editing files, running tests, or building the project.",
        "ULTRA IMPORTANT: before exiting successfully, submit completion evidence with `curl` exactly as shown below.",
        "ULTRA IMPORTANT: if `curl` is missing or the request fails, treat that as a blocking runtime error and report it instead of exiting successfully.",
        "",
        "```sh",
        "curl -sS -X POST \\",
        "  -H \"content-type: application/json\" \\",
        "  -H \"x-open-tulid-completion-token: $OPEN_TULID_COMPLETION_TOKEN\" \\",
        "  \"$OPEN_TULID_COMPLETION_ENDPOINT\" \\",
        "  --data-binary @- <<'JSON'",
        "{",
        "    \"summary\": \"what changed\",",
        _completion_artifacts_line(required_artifacts),
        "    \"changed_files\": [\"relative/workspace/path\"],",
        "    \"validation_evidence\": {\"validation-id\": \"command/result evidence\"}",
        "}",
        "JSON",
        "```",
        "",
        f"Required validations to report: {validations}.",
        *_render_validation_detail_lines(required_validation_details),
        *_render_changed_files_required_lines(changed_files_required),
        "A zero exit code without this curl completion submission is a failed Tulid job.",
    ]
    return f"{prompt_text.rstrip()}\n\n" + "\n".join(lines)


def _runtime_prompt_variant(
    *,
    transition_id: str,
    required_artifacts: tuple[str, ...],
    derived_artifact_type: str | None,
) -> str:
    if derived_artifact_type is not None:
        return "planning"
    normalized = transition_id.strip().lower()
    if not required_artifacts and (
        "implement" in normalized
        or "review" in normalized
    ):
        return "implementation"
    return "planning"


def _render_prompt_role_section(variant: str) -> str:
    if variant == "implementation":
        lines = (
            "## Role",
            "You are implementing one already-derived scoped task inside an existing plan.",
            "Do not switch into planning mode and do not broaden scope beyond the current task.",
        )
    else:
        lines = (
            "## Role",
            "You are executing a planning or artifact-producing workflow transition for this project.",
            "Synthesize the required project artifacts for this transition and keep the result aligned with the defined workflow state change.",
        )
    return "\n".join(lines)


def _render_prompt_primary_objective_section(variant: str) -> str:
    if variant == "implementation":
        lines = (
            "## Primary Objective",
            "Complete the implementation work defined by the current task body in this workspace.",
            "Make the required code or test changes, satisfy the required validations, and submit explicit completion evidence.",
            "Success for this transition is not producing new planning artifacts.",
        )
    else:
        lines = (
            "## Primary Objective",
            "Produce the planning artifacts and completion evidence required by this transition.",
            "Use the provided project context to synthesize the next workflow artifacts without skipping required deliverables.",
        )
    return "\n".join(lines)


def _render_prompt_priority_section(variant: str) -> str:
    if variant == "implementation":
        lines = (
            "## Context Priority",
            "1. The current task body is the authoritative scope boundary for this job.",
            "2. Required validations and completion requirements are mandatory.",
            "3. Parent and linked context are background reference material only.",
            "4. If reference material suggests broader project work, stay within the current task instead of expanding scope.",
        )
    else:
        lines = (
            "## Context Priority",
            "1. The transition objective and required artifacts define the deliverable for this job.",
            "2. The current task body and linked context describe the project state you must synthesize from.",
            "3. Parent context provides background project intent and continuity.",
            "4. Completion requirements remain mandatory even for planning transitions.",
        )
    return "\n".join(lines)


def _render_prompt_paths_section(variant: str, *, required_artifacts: tuple[str, ...]) -> str:
    lines = [
        "## Read-Only And Writable Paths",
        "Read `.open-tulid/job-context.json` before making changes.",
    ]
    if variant == "implementation":
        lines.extend((
            "Planning and specification documents in the workspace are read-only reference context.",
            "Source files and test files in the workspace are writable implementation targets.",
            "Use `output/` only for required completion artifacts.",
        ))
        if required_artifacts:
            lines.append("This implementation transition requires explicit artifacts under `output/`.")
        else:
            lines.append("This implementation transition does not require artifacts, so leave `output/` alone unless Tulid explicitly requires it.")
    else:
        lines.extend((
            "Use workspace files as needed to complete this transition.",
            "If repository files are present in the workspace, inspect them before writing product, technical, implementation, or breakdown artifacts.",
            "Treat repository source files as read-only context for planning transitions; write required artifacts under `output/`.",
            "Write required completion artifacts under `output/`.",
        ))
    return "\n".join(lines)


def _render_validation_failure_policy_section(variant: str) -> str:
    if variant != "implementation":
        return "## Validation Failure Policy\nUse required validations as evidence for the transition deliverable."
    return "\n".join((
        "## Validation Failure Policy",
        "Use validation failures as diagnosis, not permission to rewrite unrelated code.",
        "Before changing code because of a failing validation command, decide whether the failure is inside the assigned task boundary.",
        "If the failure is in scope, make the smallest targeted fix and rerun the narrowest relevant command first.",
        "If the failure is outside scope, pre-existing, environmental, flaky, or caused by a missing external service, do not chase it with broad edits.",
        "After one full validation failure, switch to the smallest failing test, module, or command that explains the problem.",
        "Stop instead of thrashing when the same validation failure remains after two targeted fix attempts, the fix requires files outside scope, or the failure is unrelated to this task.",
        "When stopping, exit non-zero with a concise blocker summary unless Tulid can accept the transition with precise evidence.",
    ))


def _render_prompt_completion_section(
    *,
    completion_endpoint: str,
    required_artifacts: tuple[str, ...],
    required_validations: tuple[str, ...],
    required_validation_details: tuple[str, ...],
    changed_files_required: bool,
    derived_artifact_type: str | None,
    artifacts: str,
    validations: str,
) -> str:
    lines = [
        "## Completion Contract",
        f"Required artifacts: {artifacts}",
        f"Required validations: {validations}",
    ]
    if required_artifacts:
        lines.append("Only create the artifact files explicitly required for this transition.")
    else:
        lines.extend((
            "No artifacts are required for this transition. Submit an empty `artifacts` array.",
            "Treat existing files under `output/` as read-only context unless this transition explicitly requires output artifacts.",
            "Do not regenerate product specs, technical directions, implementation specs, or task breakdown files for an implementation transition.",
        ))
    if derived_artifact_type:
        lines.extend((
            f"This transition derives child tasks via `{derived_artifact_type}` artifacts.",
            f"Submit one artifact entry per generated `{derived_artifact_type}` file.",
            "If you generate multiple task files, every generated file must appear in the `artifacts` array.",
            "Only submitted derived-task artifacts will be promoted and turned into tasks.",
        ))
    lines.extend((
        "Completion is not implied by process exit code or workspace edits alone.",
        "Use `changed_files` for every workspace path you modified outside `output/`.",
        "Use `validation_evidence` to report concrete command or result evidence for each required validation.",
        *_render_validation_detail_lines(required_validation_details),
        *_render_changed_files_required_lines(changed_files_required),
        "",
        "ULTRA IMPORTANT: when ready, submit completion evidence with `curl`.",
        "Do not exit successfully until the curl request has been made and accepted.",
        "If the response says `completion.in_progress`, remain active and wait for the final completion response instead of exiting.",
        "",
        "```sh",
        "curl -sS -X POST \\",
        "  -H \"content-type: application/json\" \\",
        "  -H \"x-open-tulid-completion-token: $OPEN_TULID_COMPLETION_TOKEN\" \\",
        "  \"$OPEN_TULID_COMPLETION_ENDPOINT\" \\",
        "  --data-binary @- <<'JSON'",
        "{",
        "    \"summary\": \"what changed\",",
        _completion_artifacts_line(required_artifacts),
        "    \"changed_files\": [\"relative/workspace/path\"],",
        "    \"validation_evidence\": {\"validation-id\": \"command/result evidence\"}",
        "}",
        "JSON",
        "```",
        "",
        "If completion is rejected, use the returned errors as feedback, fix the workspace, and submit again.",
        "If completion is still in progress, remain active and wait for the final completion response instead of exiting.",
    ))
    return "\n".join(lines)


def _render_validation_detail_lines(required_validation_details: tuple[str, ...]) -> tuple[str, ...]:
    if not required_validation_details:
        return ()
    return (
        "",
        "Required validation commands:",
        *tuple(f"- {detail}" for detail in required_validation_details),
    )


def _render_changed_files_required_lines(changed_files_required: bool) -> tuple[str, ...]:
    if not changed_files_required:
        return ()
    return (
        "",
        "`changed_files` is required for this transition. Do not submit completion with an empty `changed_files` array.",
    )


def _completion_artifacts_line(required_artifacts: tuple[str, ...]) -> str:
    if not required_artifacts:
        return '    "artifacts": [],'
    artifact_example = f'{{"type": "{required_artifacts[0]}", "path": "relative/path/in/output"}}'
    return f'    "artifacts": [{artifact_example}],'


def _worker_args(
    *,
    runtime: RuntimeConfig,
    worker_id: str,
    implementation_id: str,
    container_workspace: str,
    completion_endpoint: str,
) -> tuple[str, ...]:
    args = runtime.worker_args.get(worker_id, runtime.worker_args.get(implementation_id, ()))
    values = {
        "prompt_packet": f"{container_workspace}/.open-tulid/prompt-packet.md",
        "job_context": f"{container_workspace}/.open-tulid/job-context.json",
        "completion_endpoint": completion_endpoint,
        "workspace": container_workspace,
        "output_dir": f"{container_workspace}/output",
    }
    return tuple(arg.format(**values) for arg in args)


def _write_opencode_model_config_if_needed(
    *,
    workspace: Path,
    runtime: RuntimeConfig,
    worker_id: str,
    implementation_id: str,
    args: tuple[str, ...],
    env: Mapping[str, str],
) -> None:
    worker_type = runtime.worker_types.get(worker_id, runtime.worker_types.get(implementation_id, implementation_id))
    if worker_type != "opencode" and implementation_id != "opencode":
        return
    endpoint = env.get("OPEN_TULID_MODEL_ENDPOINT")
    proxy_id = env.get("OPEN_TULID_MODEL_PROXY_ID")
    if not endpoint or not proxy_id or "OPEN_TULID_MODEL_SESSION_TOKEN" not in env:
        return
    model_ref = _model_arg(args)
    if model_ref is None or "/" not in model_ref:
        return
    provider_id, model_id = model_ref.split("/", 1)
    expected_provider_id = f"tulid-{proxy_id}"
    if provider_id != expected_provider_id or not model_id:
        return

    path = workspace / "opencode.json"
    config: dict[str, object] = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
    except (OSError, json.JSONDecodeError):
        config = {}

    providers = config.get("provider")
    if not isinstance(providers, dict):
        providers = {}
    providers[provider_id] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": f"Tulid {proxy_id}",
        "options": {
            "baseURL": endpoint,
            "apiKey": "{env:OPEN_TULID_MODEL_SESSION_TOKEN}",
        },
        "models": {
            model_id: {
                "name": model_id,
            },
        },
    }
    config["$schema"] = "https://opencode.ai/config.json"
    config["provider"] = providers
    config["model"] = model_ref
    config["small_model"] = model_ref
    agents = config.get("agent")
    if not isinstance(agents, dict):
        agents = {}
    agents[OPENCODE_TULID_AGENT] = {
        "mode": "primary",
        "permission": {
            "*": "allow",
            "doom_loop": "deny",
        },
    }
    config["agent"] = agents
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _model_arg(args: tuple[str, ...]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--model" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--model="):
            return arg.removeprefix("--model=")
    return None


def _execution_worker_id(worker, fallback: str) -> str:
    implementation_id = getattr(worker, "implementation_id", None)
    return str(implementation_id) if implementation_id else fallback


def _adapter_project_root(adapter: StorageAdapter) -> Path | None:
    config = getattr(adapter, "config", None)
    project_root = getattr(config, "project_root", None)
    return project_root if isinstance(project_root, Path) else None


def _load_parent_tasks(adapter: StorageAdapter, task: Task) -> tuple[Task, ...]:
    parent_id = task.parent_id
    if not parent_id or parent_id == task.id:
        return ()
    loaded = adapter.read_task(parent_id)
    if not loaded.accepted or loaded.task is None:
        return ()
    return (loaded.task,)


def _append_parent_tasks(prompt_text: str, parent_tasks: tuple[Task, ...]) -> str:
    if not parent_tasks:
        return prompt_text
    sections = []
    for index, task in enumerate(parent_tasks, start=1):
        sections.append(
            "\n".join((
                f"## Parent Context {index}",
                "This section is background project context, not an instruction to broaden the assigned task.",
                f"ID: {task.id}",
                f"Title: {task.title}",
                "",
                sanitize_task_body_for_runtime(task.body).strip(),
            ))
        )
    return f"{prompt_text}\n\n" + "\n\n".join(sections)


def _task_for_prompt_context(task: Task, transition: TransitionDefinition) -> Task:
    excluded_artifact_types = set(transition.requires.artifacts)
    if transition.derives is not None:
        excluded_artifact_types.add(transition.derives.artifact_type)
    artifact_links = tuple(
        link for link in task.artifact_links
        if _artifact_type_from_link(link) not in excluded_artifact_types
    )
    return Task(
        id=task.id,
        title=task.title,
        path=task.path,
        current_state=task.current_state,
        task_type=task.task_type,
        dependencies=task.dependencies,
        artifact_links=artifact_links,
        parent_id=task.parent_id,
        metadata=task.metadata,
        body=task.body,
    )


def _artifact_type_from_link(link: str) -> str | None:
    parts = Path(link).parts
    try:
        index = parts.index("artifacts")
    except ValueError:
        return None
    if index + 2 >= len(parts):
        return None
    return parts[index + 2]


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
