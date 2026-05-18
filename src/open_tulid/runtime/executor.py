from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Mapping

from open_tulid.adapters.base import StorageAdapter
from open_tulid.containers.runtime import AgentRunResult, ContainerMount, request_for_worker, run_agent_container
from open_tulid.domain import DomainError, EventActor, EventType, ExecutionJobStatus, Task, WorkflowDefinition
from open_tulid.models import ModelProxyConfig, ProjectConfig, ResourceConfig, RuntimeConfig
from open_tulid.runtime.completion import CompletionService
from open_tulid.runtime.completion_http import CompletionEndpointConfig, serve_completion_endpoint
from open_tulid.runtime.events import JsonlEventStore, TransactionJournalStore, build_event
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.instructions import AgentInstructionResolver
from open_tulid.runtime.context import LinkedContextResolver
from open_tulid.runtime.resources import FileResourceLeaseStore
from open_tulid.runtime.model_proxy import FileModelProxySessionStore, ModelProxySessionStore
from open_tulid.runtime.verifier import CompletionSubmission
from open_tulid.runtime.workspaces import WorkspacePreparer

TERMINAL_JOB_STATUSES = frozenset({
    ExecutionJobStatus.ACCEPTED.value,
    ExecutionJobStatus.FAILED.value,
    ExecutionJobStatus.STALE.value,
    ExecutionJobStatus.CANCELLED.value,
})


@dataclass(frozen=True)
class ExecutorRunResult:
    accepted: bool
    run: AgentRunResult | None = None
    errors: tuple[DomainError, ...] = ()


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
        task_type = self.workflow.task_types.get(transition.task_type)
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

            prompt_packet = None
            prompt_text = _build_runtime_prompt(
                job_id=job.job_id,
                task_title=task_result.task.title,
                task_body=task_result.task.body,
                transition_id=transition.id,
                from_state=transition.from_state,
                to_state=transition.to_state,
                required_artifacts=transition.requires.artifacts,
                required_validations=tuple(call.type for call in transition.requires.validations),
                completion_endpoint=endpoint.url,
            )
            project_root = _adapter_project_root(self.adapter)
            if project_root is not None:
                parent_tasks = _load_parent_tasks(self.adapter, task_result.task)
                prompt_text = _append_parent_tasks(prompt_text, parent_tasks)
                context_result = LinkedContextResolver(project_root).build_context_packet(
                    task_result.task,
                    parent_tasks=parent_tasks,
                )
                if not context_result.accepted:
                    endpoint.stop()
                    return self._fail_before_run(
                        job,
                        context_result.errors[0],
                    )
                context_packet = context_result.packet
                if context_packet is not None and context_packet.text:
                    prompt_text = f"{prompt_text}\n\n{context_packet.text}"
                prompt_result = AgentInstructionResolver(project_root).build_prompt_packet(
                    worker=worker,
                    task_type=task_type,
                    transition=transition,
                )
                if not prompt_result.accepted:
                    endpoint.stop()
                    return self._fail_before_run(
                        job,
                        prompt_result.errors[0] if prompt_result.errors else _error(
                            "instructions.invalid",
                            "Prompt instructions failed.",
                        ),
                    )
                prompt_packet = prompt_result.packet
                if prompt_packet is not None:
                    prompt_text = f"{prompt_text}\n\n{prompt_packet.text}"
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

            request = request_for_worker(
                worker_id=_execution_worker_id(worker, job.worker_id),
                workspace=prepared.workspace,
                runtime=self.runtime,
                args=_worker_args(
                    runtime=self.runtime,
                    worker_id=job.worker_id,
                    implementation_id=_execution_worker_id(worker, job.worker_id),
                    container_workspace=self.runtime.container_workspace,
                    completion_endpoint=endpoint.url,
                ),
                env={
                    "OPEN_TULID_JOB_ID": job.job_id,
                    "OPEN_TULID_COMPLETION_TOKEN": str(job.metadata.get("completion_token", "")),
                    "OPEN_TULID_OUTPUT_DIR": f"{self.runtime.container_workspace}/output",
                    "OPEN_TULID_COMPLETION_ENDPOINT": endpoint.url,
                    "OPEN_TULID_PROMPT_PACKET": f"{self.runtime.container_workspace}/.open-tulid/prompt-packet.md",
                    **self._model_proxy_env(job.job_id, job.worker_id, required_resources),
                },
                mounts=self._subscription_mounts(required_resources),
            )
            try:
                result = run_agent_container(request, docker_executable=self.runtime.docker_executable)
            finally:
                endpoint.stop()
            _write_run_logs(Path(job.workspace_path), result)
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
            if result.succeeded and self._try_implicit_completion(
                job_id=job.job_id,
                transition=transition,
                workspace=Path(job.workspace_path),
                token=str(job.metadata.get("completion_token", "")),
            ):
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

    def _try_implicit_completion(
        self,
        *,
        job_id: str,
        transition,
        workspace: Path,
        token: str,
    ) -> bool:
        if transition.requires.artifacts or transition.requires.validations or transition.derives is not None:
            return False
        changed_files = _git_changed_files(workspace)
        if changed_files is None:
            changed_files = _workspace_changed_files(
                workspace=workspace,
                repo_root=self.project_config.repo_root,
            )
        if changed_files is None:
            return False
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
        result = service.submit(
            job_id=job_id,
            token=token,
            submission=CompletionSubmission(
                summary="Worker exited successfully; completion evidence inferred by Tulid.",
                changed_files=tuple(sorted(changed_files)),
            ),
        )
        return result.accepted

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


def _write_run_logs(workspace: Path, result: AgentRunResult) -> None:
    log_dir = workspace / ".open-tulid" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (log_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    (log_dir / "command.txt").write_text(" ".join(_redact_command_for_log(result.command)) + "\n", encoding="utf-8")


def _git_changed_files(workspace: Path) -> set[str] | None:
    if not (workspace / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    changed_files: set[str] = set()
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed_files.add(path)
    return changed_files


_WORKSPACE_DIFF_IGNORES = frozenset({
    ".git",
    ".open-tulid",
    "output",
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
})


def _workspace_changed_files(*, workspace: Path, repo_root: Path | None) -> set[str] | None:
    if repo_root is None or not repo_root.is_dir():
        return None
    changed_files: set[str] = set()
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if any(part in _WORKSPACE_DIFF_IGNORES for part in relative.parts):
            continue
        repo_path = repo_root / relative
        if not repo_path.is_file() or _sha256(path) != _sha256(repo_path):
            changed_files.add(str(relative))
    return changed_files


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    completion_endpoint: str,
) -> str:
    artifacts = ", ".join(required_artifacts) if required_artifacts else "none"
    validations = ", ".join(required_validations) if required_validations else "none"
    return "\n".join((
        "# Open Tulid Job",
        "",
        f"Job: {job_id}",
        f"Task: {task_title}",
        f"Transition: {transition_id} ({from_state} -> {to_state})",
        "",
        "Read `.open-tulid/job-context.json` before making changes.",
        "Make the requested code changes in this workspace.",
        "Write required completion artifacts under `output/`.",
        f"Required artifacts: {artifacts}",
        f"Required validations: {validations}",
        "",
        "When ready, submit completion evidence with:",
        "",
        "```sh",
        "curl -sS -X POST \\",
        "  -H \"content-type: application/json\" \\",
        "  -H \"x-open-tulid-completion-token: $OPEN_TULID_COMPLETION_TOKEN\" \\",
        f"  \"$OPEN_TULID_COMPLETION_ENDPOINT\" \\",
        "  -d '{",
        "    \"summary\": \"what changed\",",
        "    \"artifacts\": [{\"type\": \"artifact-type\", \"path\": \"relative/path/in/output\"}],",
        "    \"changed_files\": [\"relative/workspace/path\"],",
        "    \"validation_evidence\": {\"validation-id\": \"command/result evidence\"}",
        "  }'",
        "```",
        "",
        "If completion is rejected, use the returned errors as feedback, fix the workspace, and submit again.",
        "",
        "## Task Body",
        task_body.strip(),
    ))


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


def _execution_worker_id(worker, fallback: str) -> str:
    implementation_id = getattr(worker, "implementation_id", None)
    return str(implementation_id) if implementation_id else fallback


def _adapter_project_root(adapter: StorageAdapter) -> Path | None:
    config = getattr(adapter, "config", None)
    project_root = getattr(config, "project_root", None)
    return project_root if isinstance(project_root, Path) else None


def _load_parent_tasks(adapter: StorageAdapter, task: Task) -> tuple[Task, ...]:
    parents: list[Task] = []
    seen: set[str] = {task.id}
    parent_id = task.parent_id
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        loaded = adapter.read_task(parent_id)
        if not loaded.accepted or loaded.task is None:
            break
        parents.append(loaded.task)
        parent_id = loaded.task.parent_id
    return tuple(parents)


def _append_parent_tasks(prompt_text: str, parent_tasks: tuple[Task, ...]) -> str:
    if not parent_tasks:
        return prompt_text
    sections = []
    for index, task in enumerate(parent_tasks, start=1):
        sections.append(
            "\n".join((
                f"## Parent Task {index}",
                f"ID: {task.id}",
                f"Title: {task.title}",
                "",
                task.body.strip(),
            ))
        )
    return f"{prompt_text}\n\n" + "\n\n".join(sections)


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
