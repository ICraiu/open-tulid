from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace as _replace_request
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
    ExecutionJob,
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
from open_tulid.runtime.execution_contracts import (
    ExecutionContract,
    load_job_execution_contract,
    source_content_identities,
)
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.instructions import AgentInstructionResolver, PromptPacket
from open_tulid.runtime.prompts import (
    CompiledPrompt,
    PromptBudgetError,
    ReviewEvidence,
    compile_execution_prompt,
    compiled_prompt_from_metadata,
)
from open_tulid.runtime.context import (
    LinkedContextResolver,
    load_parent_tasks,
    sanitize_task_body_for_runtime,
    task_for_context,
)
from open_tulid.runtime.resources import FileResourceLeaseStore
from open_tulid.runtime.model_proxy import FileModelProxySessionStore, ModelProxySessionStore
from open_tulid.runtime.failures import ExecutionFailure, classify_worker_failure
from open_tulid.runtime.observability import (
    WorkerExited,
    WorkerLivenessProbe,
    WorkerObservability,
)
from open_tulid.runtime.attempts import (
    AttemptRecord,
    AttemptStatus,
    attempt_deadline,
    attempt_id_for,
    attempt_record_to_dict,
    attempt_records_from_metadata,
    count_consumed_attempts,
    task_semantic_revision,
)
from open_tulid.runtime.baseline import (
    RuntimeBaseline,
    baseline_to_dict,
    capture_runtime_baseline,
)
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
_WORKER_POLL_SECONDS = 0.05
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
    execution_contract: ExecutionContract | None = None
    execution_contract_sha256: str | None = None
    compiled_prompt: CompiledPrompt | None = None
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
    execution_contract: ExecutionContract | None = None,
    review_evidence: ReviewEvidence | None = None,
) -> PromptRenderResult:
    """Render the exact model prompt for an execution job without running it."""
    if execution_contract is not None:
        try:
            compiled = compile_execution_prompt(
                execution_contract,
                review_evidence=review_evidence,
            )
        except PromptBudgetError as exc:
            return PromptRenderResult(errors=(_error(
                exc.code,
                str(exc),
                task.id,
            ),))
        except ValueError as exc:
            return PromptRenderResult(errors=(_error(
                "prompt.compile_failed",
                str(exc),
                task.id,
            ),))
        return PromptRenderResult(
            text=compiled.text,
            execution_contract=execution_contract,
            execution_contract_sha256=execution_contract.sha256,
            compiled_prompt=compiled,
        )
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
        derived_artifact_type=transition.derives.artifact_type if transition.derives is not None else None,
    )
    prompt_packet = None
    parent_tasks: tuple[Task, ...] = ()
    context_packet = None
    context_task = task
    project_root = _adapter_project_root(adapter)
    if project_root is not None:
        parent_tasks = load_parent_tasks(adapter, task)
        context_task = task_for_context(task, transition)
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
        context_result = LinkedContextResolver(project_root).build_context_packet(
            context_task,
            parent_tasks=parent_tasks,
        )
        if not context_result.accepted:
            return PromptRenderResult(errors=context_result.errors)
        context_packet = context_result.packet
    prompt_text = _append_completion_submission(
        prompt_text,
        required_artifacts=transition.requires.artifacts,
        required_validations=_validation_ids(transition.requires.validations),
        required_validation_details=_validation_details(transition.requires.validations),
        changed_files_required=transition.requires.changed_files_required,
        derived_artifact_type=transition.derives.artifact_type if transition.derives is not None else None,
        derived_artifact_required=transition.derives.required if transition.derives is not None else True,
        parent_to_if_derived=(
            transition.derives.parent_to_if_derived
            if transition.derives is not None
            else None
        ),
    )
    # Planning transitions (specification and breakdown) additionally receive
    # repository facts and the unfinished task inventory before any parent or
    # linked-reference context, so the worker can distinguish implemented
    # behavior from remaining work and avoid duplicate ownership.
    planning_inputs = _planning_inputs_text(
        adapter,
        project_root,
        transition,
        context_task,
        parent_tasks,
    )
    if planning_inputs:
        prompt_text = f"{prompt_text}\n\n{planning_inputs}"
    # Operative worker and completion instructions must precede potentially
    # very large parent and linked-reference context so the model sees the
    # entire execution contract before it begins acting on background material.
    prompt_text = _append_parent_tasks(prompt_text, parent_tasks)
    if context_packet is not None and context_packet.text:
        prompt_text = f"{prompt_text}\n\n{context_packet.text}"
    return PromptRenderResult(
        text=prompt_text,
        instruction_packet=prompt_packet,
        execution_contract_sha256=(
            execution_contract.sha256
            if execution_contract is not None
            else None
        ),
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
        proxy_evidence_root: Path | None = None,
        validation_implementations: Mapping[str, object] | None = None,
        validation_context_factory: object | None = None,
        completion_settle_timeout_seconds: float = DEFAULT_COMPLETION_SETTLE_TIMEOUT_SECONDS,
        containers: ContainersService | None = None,
        observability: WorkerObservability | None = None,
        worker_liveness_check_interval_seconds: float | None = None,
        liveness_probe: WorkerLivenessProbe | None = None,
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
        self.proxy_evidence_root = proxy_evidence_root
        self.validation_implementations = validation_implementations
        self.validation_context_factory = validation_context_factory
        self.completion_settle_timeout_seconds = completion_settle_timeout_seconds
        self.containers = containers or build_containers_service()
        if observability is not None:
            self.observability = observability
        else:
            interval = (
                worker_liveness_check_interval_seconds
                if worker_liveness_check_interval_seconds is not None
                else getattr(
                    runtime,
                    "worker_liveness_check_interval_seconds",
                    60.0,
                )
            )
            self.observability = WorkerObservability(check_interval_seconds=interval)
        self.liveness_probe = liveness_probe

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
        frozen = load_job_execution_contract(job)
        if not frozen.accepted:
            return self._fail_before_run(job, frozen.errors[0])
        transition = (
            frozen.contract.transition
            if frozen.contract is not None
            else self.workflow.transitions.get(job.transition_id)
        )
        if transition is None:
            return ExecutorRunResult(False, errors=(_error(
                "transition.not_found",
                "Transition was not found.",
            ),))
        worker = self.workflow.workers.get(job.worker_id)
        task_result = self.adapter.read_task(job.task_id)
        if not task_result.accepted or task_result.task is None:
            return ExecutorRunResult(False, errors=task_result.errors or (_error("task.not_found", "Task was not found."),))
        execution_task = (
            frozen.contract.source_task
            if frozen.contract is not None
            else task_result.task
        )

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

        settled_attempt_id: str | None = None
        try:
            endpoint = self._start_completion_endpoint(job.job_id)
            repair_mode = (
                job_status == ExecutionJobStatus.COMPLETION_REJECTED.value
                and bool(job.metadata.get("repair_ready"))
            )
            prepared = WorkspacePreparer(repo_root=self.project_config.repo_root).prepare(
                job=job,
                task=execution_task,
                transition=transition,
                completion_endpoint=endpoint.url,
                preserve_workspace=repair_mode,
            )
            if not prepared.accepted or prepared.workspace is None:
                endpoint.stop()
                return self._fail_before_run(
                    job,
                    prepared.error or _error("workspace.prepare_failed", "Workspace failed."),
                )

            rendered_prompt = None
            try:
                prompt_text = (
                    _repair_prompt_packet(job)
                    if repair_mode
                    else _frozen_prompt_packet(job, frozen.contract)
                )
            except ValueError as exc:
                endpoint.stop()
                return self._fail_before_run(job, _error(
                    "prompt.frozen_invalid",
                    str(exc),
                    job.job_id,
                ))
            prompt_packet = None
            if prompt_text is None:
                rendered_prompt = render_execution_prompt(
                    workflow=self.workflow,
                    adapter=self.adapter,
                    task=execution_task,
                    transition=transition,
                    worker_id=job.worker_id,
                    job_id=job.job_id,
                    completion_endpoint=endpoint.url,
                    execution_contract=frozen.contract,
                )
                if not rendered_prompt.accepted:
                    endpoint.stop()
                    return self._fail_before_run(job, rendered_prompt.errors[0])
                prompt_text = rendered_prompt.text
                prompt_packet = rendered_prompt.instruction_packet
            prompt_sha256 = _write_prompt_packet(prepared.workspace, prompt_text)
            if rendered_prompt is not None and rendered_prompt.compiled_prompt is not None:
                _write_prompt_manifest(prepared.workspace, rendered_prompt.compiled_prompt)
            elif isinstance(job.metadata.get("prompt_manifest"), Mapping):
                _write_prompt_manifest_payload(prepared.workspace, job.metadata["prompt_manifest"])

            attempt_record = self._admit_attempt(
                job,
                execution_task,
                workspace=prepared.workspace,
                source_identities=source_content_identities(frozen.contract)
                if frozen.contract is not None
                else (),
            )
            settled_attempt_id = attempt_record.attempt_id
            self.job_store.update_status(
                job.job_id,
                ExecutionJobStatus.RUNNING,
                metadata={
                    "workspace_prepared": True,
                    "completion_endpoint": endpoint.url,
                    "completion_endpoint_host": endpoint.host,
                    "completion_endpoint_port": endpoint.port,
                    "prompt_packet_sha256": (
                        job.metadata.get("prompt_packet_sha256")
                        if repair_mode else prompt_sha256
                    ),
                    "instruction_packet_sha256": prompt_packet.sha256 if prompt_packet is not None else None,
                    "prompt_manifest": (
                        rendered_prompt.compiled_prompt.manifest.to_dict()
                        if rendered_prompt is not None and rendered_prompt.compiled_prompt is not None else job.metadata.get("prompt_manifest")
                    ),
                    **({"repair_ready": False, "repair_attempts": int(job.metadata.get("repair_attempts", 0)) + 1} if repair_mode else {}),
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
            model_proxy_env = self._model_proxy_env(
                job.job_id,
                job.worker_id,
                required_resources,
                attempt=attempt_record,
            )
            standard_runtime = _project_standard_runtime(self.adapter)
            worker_args = _worker_args(
                runtime=self.runtime,
                worker_id=job.worker_id,
                implementation_id=implementation_id,
                container_workspace=self.runtime.container_workspace,
                completion_endpoint=endpoint.url,
            )
            opencode_config = _write_opencode_model_config_if_needed(
                workspace=prepared.workspace,
                runtime=self.runtime,
                worker_id=job.worker_id,
                implementation_id=implementation_id,
                args=worker_args,
                env=model_proxy_env,
                opencode_config_home=standard_runtime.opencode_config_home,
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
                    **({"OPENCODE_CONFIG": opencode_config} if opencode_config is not None else {}),
                    **model_proxy_env,
                },
                mounts=self._subscription_mounts(required_resources),
            )
            request = _replace_request(
                request,
                container_user=standard_runtime.container_user or request.container_user,
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
            self._mark_attempt_running(job.job_id, attempt_record.attempt_id)
            try:
                result = self._run_worker_monitored(
                    job=job,
                    request=request,
                    log_dir=log_dir,
                )
                if result is not None and result.succeeded:
                    self._wait_for_completion_settlement(job.job_id)
            finally:
                endpoint.stop()
            finished_at = _utc_now()
            if result is None:
                # The worker vanished before it had an accepted completion. The
                # failure flow already failed the job, stopped/cleaned the
                # worker, preserved its workspace and evidence, and released its
                # lease. The task itself remains in its existing state for a
                # fresh attempt.
                return ExecutorRunResult(True, run=None)
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
            if status_after_run == ExecutionJobStatus.COMPLETION_REJECTED.value:
                repaired = self.job_store.get(job.job_id)
                if (
                    repaired.accepted
                    and repaired.job is not None
                    and repaired.job.metadata.get("repair_ready") is True
                ):
                    if self._repair_within_total_account(
                        job,
                        execution_task,
                        source_identities=(
                            source_content_identities(frozen.contract)
                            if frozen.contract is not None
                            else ()
                        ),
                    ):
                        # A rejected completion is feedback, not task completion.
                        # Restart the same frozen job in its preserved workspace so
                        # the worker receives the structured repair packet and can
                        # submit a new completion without a daemon tick/manual run.
                        return self.run(job.job_id)
                    # The durable total attempt account is exhausted; a repair
                    # would exceed the bounded budget, so settle the job instead
                    # of starting another worker process.
                    self._fail_at_total_attempt_bound(
                        job,
                        revision=self._task_revision(
                            execution_task,
                            contract=frozen.contract,
                        ),
                    )
                return ExecutorRunResult(True, run=result)
            if status_after_run in {
                ExecutionJobStatus.FAILED.value,
                ExecutionJobStatus.STALE.value,
                ExecutionJobStatus.CANCELLED.value,
            }:
                # A terminal outcome was already recorded; preserve the failed
                # workspace and its evidence so recoverable work never
                # disappears before recovery evidence is retained.
                self._preserve_failure_evidence(job)
                return ExecutorRunResult(True, run=result)
            # The worker exited without an accepted completion. That is a faulty
            # worker: fail the job, preserve its evidence, and release its lease
            # so a fresh scheduler attempt can reuse the same task.
            return self._fail_completed_worker_without_completion(job, result)
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
            if settled_attempt_id is not None:
                self._settle_attempt(job.job_id, settled_attempt_id)
            if lease_acquired and self.lease_store is not None:
                self.lease_store.release_job(job.job_id)
            if self.model_proxy_sessions is not None:
                if settled_attempt_id is not None:
                    self.model_proxy_sessions.revoke_attempt(settled_attempt_id)
                else:
                    self.model_proxy_sessions.revoke_job(job.job_id)

    def _admit_attempt(self, job, task, *, workspace: Path, source_identities=()) -> AttemptRecord:
        """Persist a versioned attempt admission before spawning the worker.

        The record is written under the already-held lease/store coordination
        and carries the semantic task revision, transition, attempt number,
        predecessor, worker identity, start time, and explicit deadline. A
        baseline for the isolated fixture run is captured and persisted to the
        workspace so the run is reproducible and the installed runtime is never
        assumed to be the checkout being edited.
        """
        now = _utc_now()
        attempt_number = int(job.attempts) + 1
        attempt_id = attempt_id_for(job.job_id, attempt_number)
        predecessor = self._preceding_attempt_id(job, attempt_number)
        record = AttemptRecord(
            schema="tulid.attempt/v1",
            attempt_id=attempt_id,
            job_id=job.job_id,
            attempt_number=attempt_number,
            task_revision=self._task_revision(task, source_identities=source_identities),
            transition_id=job.transition_id,
            worker_id=job.worker_id,
            predecessor=predecessor,
            status=AttemptStatus.ADMITTED,
            started_at=now.isoformat(),
            deadline=attempt_deadline(
                started_at=now,
                attempt_duration_seconds=self.runtime.default_timeout_seconds,
                settlement_allowance_seconds=max(0.0, self.completion_settle_timeout_seconds),
            ),
            metadata={"baseline": baseline_to_dict(self._capture_baseline(job, workspace))},
        )
        persisted = self.job_store.record_attempt(job.job_id, attempt_record_to_dict(record))
        if persisted.accepted:
            self._write_baseline_file(record, workspace)
        else:
            raise ValueError(
                f"Cannot admit attempt for job {job.job_id!r}: "
                f"{persisted.error.message if persisted.error else 'unknown failure'}"
            )
        return record

    def _capture_baseline(self, job, workspace: Path) -> RuntimeBaseline:
        project_root = getattr(self.project_config, "repo_root", None)
        command_policy_hash = self._command_policy_hash()
        baseline = capture_runtime_baseline(
            source_root=project_root,
            workflow=self.workflow,
            worker_id=job.worker_id,
            worker_images=getattr(self.runtime, "worker_images", {}),
            worker_types=getattr(self.runtime, "worker_types", {}),
            worker_args=getattr(self.runtime, "worker_args", {}),
            default_timeout_seconds=self.runtime.default_timeout_seconds,
            max_repair_attempts=getattr(self.runtime, "max_repair_attempts", 0),
            command_policy_sha256=command_policy_hash,
        )
        return baseline

    def _write_baseline_file(self, record: AttemptRecord, workspace: Path) -> None:
        baseline_payload = record.metadata.get("baseline")
        if not isinstance(baseline_payload, Mapping):
            return
        from .baseline import baseline_from_dict, write_runtime_baseline

        try:
            baseline = baseline_from_dict(baseline_payload)
        except (ValueError, TypeError):
            return
        write_runtime_baseline(baseline, workspace)

    def _command_policy_hash(self) -> str | None:
        from .repository_facts import canonical_sha256
        from .standard_contracts import load_standard_contract

        project_root = getattr(self.project_config, "repo_root", None)
        if project_root is None:
            return None
        contract_result = load_standard_contract(project_root)
        contract = getattr(contract_result, "contract", None)
        if contract is None:
            return None
        commands = sorted(
            (command.name, tuple(command.argv))
            for command in getattr(contract, "commands", ())
        )
        return canonical_sha256({"commands": commands})

    def _preceding_attempt_id(self, job, attempt_number: int) -> str | None:
        if attempt_number <= 1:
            return None
        try:
            records = attempt_records_from_metadata(job.metadata)
        except ValueError:
            return None
        previous = [record for record in records if record.attempt_number < attempt_number]
        if not previous:
            return None
        latest = max(previous, key=lambda record: record.attempt_number)
        return latest.attempt_id if latest.job_id == job.job_id else None

    def _mark_attempt_running(self, job_id: str, attempt_id: str) -> None:
        self._update_attempt(job_id, attempt_id, status=AttemptStatus.RUNNING.value)

    def _settle_attempt(self, job_id: str, attempt_id: str) -> None:
        loaded = self.job_store.get(job_id)
        status = (
            _job_status_str(loaded)
            if loaded.accepted and loaded.job is not None
            else ""
        )
        failure_reference = None
        if status == ExecutionJobStatus.FAILED.value:
            failure_reference = job_id
        self._update_attempt(
            job_id,
            attempt_id,
            status=AttemptStatus.ENDED.value,
            ended_at=_utc_now().isoformat(),
            failure_reference=failure_reference,
        )

    def _update_attempt(
        self,
        job_id: str,
        attempt_id: str,
        *,
        status: str,
        ended_at: str | None = None,
        failure_reference: str | None = None,
    ) -> None:
        loaded = self.job_store.get(job_id)
        if not loaded.accepted or loaded.job is None:
            return
        job = loaded.job
        try:
            records = attempt_records_from_metadata(job.metadata)
        except ValueError:
            return
        target = next((record for record in records if record.attempt_id == attempt_id), None)
        if target is None:
            return
        updated = _updated_attempt_record(target, status=status, ended_at=ended_at, failure_reference=failure_reference)
        self.job_store.record_attempt(job_id, attempt_record_to_dict(updated))

    def total_attempt_limit(self) -> int:
        return int(getattr(self.runtime, "max_total_attempts_per_transition", 0))

    def _task_revision(self, task, *, source_identities=(), contract=None) -> str:
        if contract is not None:
            source_identities = source_content_identities(contract)
        return task_semantic_revision(task, source_identities=source_identities)

    def _consumed_attempts(self, job, task, *, source_identities=()) -> int:
        listed = self.job_store.list()
        if not listed.accepted:
            return 0
        return count_consumed_attempts(
            jobs=listed.jobs,
            project_id=job.project_id,
            task_id=job.task_id,
            transition_id=job.transition_id,
            task_revision=self._task_revision(task, source_identities=source_identities),
        )

    def _repair_within_total_account(self, job, task, *, source_identities=()) -> bool:
        """A repair may start only while the durable total account has room.

        The account counts every persisted admission across jobs for the task
        revision and transition, so a fresh scheduler job and an in-place repair
        share one bounded budget that survives a daemon restart.
        """
        total = self.total_attempt_limit()
        if total <= 0:
            return True
        return self._consumed_attempts(job, task, source_identities=source_identities) < total

    def _fail_at_total_attempt_bound(self, job, *, revision: str) -> None:
        total = self.total_attempt_limit()
        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.FAILED,
            metadata={
                "failure_reason": "total_attempt_limit_reached",
                "failure_detail": (
                    f"Task {job.task_id!r} consumed all {total} worker "
                    f"attempt(s) for transition {job.transition_id!r}; "
                    "the durable total account is exhausted."
                ),
                "task_revision": revision,
            },
        )
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=EventActor(type="system", id="executor"),
            event_type=EventType.ExecutionFailed,
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            data={
                "reason": "total_attempt_limit_reached",
                "task_revision": revision,
                "total_attempts": total,
            },
        ))
        # Leave the workspace intact so the failed work and evidence remain
        # readable; the exhausted durable account prevents any re-admission.

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
            max_repair_attempts=self.runtime.max_repair_attempts,
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
        *,
        attempt: AttemptRecord | None = None,
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
                attempt_id=attempt.attempt_id if attempt is not None else None,
                expires_at=attempt.deadline if attempt is not None else None,
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

    def _run_worker_monitored(
        self,
        *,
        job: ExecutionJob,
        request,
        log_dir: Path,
    ):
        """Run a worker while supervising its liveness.

        The worker runs in a background thread so the executor can respond to a
        liveness failure without waiting for the blocking run to return. Liveness
        is registered with the executor-owned observability before/around the
        process start so immediate exits are caught, and it is keyed by the
        current job plus attempt identity so a stale worker/job can never affect
        the current attempt.
        """
        result_box: dict[str, object] = {}
        exit_event = threading.Event()
        exited: list[WorkerExited] = []
        run_holder: list[threading.Thread | None] = [None]

        def container_run() -> None:
            try:
                result_box["result"] = _run_agent_container_with_logs(
                    self.containers,
                    request,
                    docker_executable=self.runtime.docker_executable,
                    log_dir=log_dir,
                )
            except Exception as exc:
                result_box["error"] = exc

        attempt_id = str(job.attempts)
        if self.liveness_probe is not None:
            probe = self.liveness_probe
        else:
            probe = _RunLifecycleLivenessProbe(run_holder)

        def on_exited(event: WorkerExited) -> None:
            # A run that already finished (successful or failed) is reconciled by
            # the normal post-run/exception processing below; do not
            # double-handle it here. Only a worker that vanished while its run
            # was still in flight triggers the proactive failure path.
            if "result" in result_box or "error" in result_box:
                return
            exited.append(event)
            exit_event.set()

        thread = threading.Thread(
            target=container_run,
            name=f"open-tulid-worker-{job.job_id}",
            daemon=True,
        )
        run_holder[0] = thread
        self.observability.register(
            job_id=job.job_id,
            attempt_id=attempt_id,
            task_id=job.task_id,
            probe=probe,
            read_status=lambda job_id: _job_status_str(self.job_store.get(job_id)),
            on_exited=on_exited,
        )
        thread.start()

        try:
            while True:
                if exit_event.is_set():
                    self._fail_worker_after_unexpected_exit(job, exited[0], request=request)
                    thread.join(timeout=self._worker_stop_timeout())
                    break
                if not thread.is_alive():
                    break
                time.sleep(_WORKER_POLL_SECONDS)
        finally:
            self.observability.unregister(job_id=job.job_id)

        error = result_box.get("error")
        if "result" not in result_box and error is not None:
            raise error  # type: ignore[misc]
        return result_box.get("result")

    def _fail_worker_after_unexpected_exit(self, job, event: WorkerExited, *, request) -> None:
        self._fail_worker(
            job,
            reason="worker_unexpected_exit",
            detail=None,
            returncode=event.returncode,
            request=request,
            stop_container=True,
            preserve_evidence=True,
            failure=self._classify_vanished_failure(job),
        )

    def _fail_completed_worker_without_completion(self, job, result) -> ExecutorRunResult:
        failure = self._classify_result_failure(job, result)
        if result.succeeded:
            self._fail_worker(
                job,
                reason="completion_not_accepted",
                detail=None,
                returncode=result.returncode,
                request=None,
                stop_container=False,
                preserve_evidence=True,
                failure=failure,
            )
        else:
            self._fail_worker(
                job,
                reason=None,
                detail=None,
                returncode=result.returncode,
                request=None,
                stop_container=False,
                preserve_evidence=True,
                failure=failure,
            )
        return ExecutorRunResult(True, run=result)

    def _evidence_path(self, job) -> str | None:
        return str(Path(job.workspace_path) / ".open-tulid" / "logs" / "agent.log")

    def _classify_result_failure(
        self,
        job,
        result,
    ) -> ExecutionFailure | None:
        return classify_worker_failure(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            job_id=job.job_id,
            proxy_evidence_root=self.proxy_evidence_root,
            evidence_path=self._evidence_path(job),
        )

    def _classify_vanished_failure(self, job) -> ExecutionFailure | None:
        agent_log = Path(self._evidence_path(job) or "")
        text = ""
        if agent_log.is_file():
            try:
                text = agent_log.read_text(encoding="utf-8")
            except OSError:
                text = ""
        return classify_worker_failure(
            returncode=None,
            stdout="",
            stderr=text,
            job_id=job.job_id,
            proxy_evidence_root=self.proxy_evidence_root,
            evidence_path=self._evidence_path(job),
        )

    def _fail_worker(
        self,
        job,
        *,
        reason: str | None,
        detail: str | None,
        returncode: int | None,
        request,
        stop_container: bool,
        preserve_evidence: bool = False,
        failure: ExecutionFailure | None = None,
    ) -> None:
        """Atomically fail an orphaned/faulty worker. Idempotent and race-safe.

        Does nothing if the job already reached an accepted/terminal outcome so
        a duplicate health notification can never create duplicate retries,
        cleanup races, or clobber an accepted completion.

        When ``preserve_evidence`` is set, the failed workspace and its logs are
        retained (never scrubbed) and a durable failure-evidence record is
        persisted, so a failed worker's recoverable work and evidence outlive
        the attempt.
        """
        loaded = self.job_store.get(job.job_id)
        if not loaded.accepted or loaded.job is None:
            return
        status = _job_status_str(loaded)
        if status == ExecutionJobStatus.ACCEPTED.value:
            return
        if status in TERMINAL_JOB_STATUSES:
            return
        # A completion is being validated when the worker exit races the
        # observer. Fail only after validation resolves; doing otherwise would
        # tear an imminent acceptance out from under the validation flow.
        if status == ExecutionJobStatus.COMPLETION_SUBMITTED.value:
            return
        metadata: dict[str, object] = {"worker_returncode": returncode} if returncode is not None else {}
        event_data: dict[str, object] = {"returncode": returncode} if returncode is not None else {}
        if failure is not None:
            metadata.update({
                "failure_code": failure.code,
                "failure_category": failure.category,
                "retryable": failure.retryable,
                "retry_action": failure.retry_action,
                "failure_evidence": failure.evidence,
                "failure_evidence_path": failure.evidence_path,
            })
            event_data.update({
                "failure_code": failure.code,
                "failure_category": failure.category,
                "retryable": failure.retryable,
                "retry_action": failure.retry_action,
            })
        if reason is not None:
            metadata["failure_reason"] = reason
            event_data["reason"] = reason
        if detail is not None:
            metadata["failure_detail"] = detail
        self.job_store.update_status(job.job_id, ExecutionJobStatus.FAILED, metadata=metadata)
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=EventActor(type="system", id="executor"),
            event_type=EventType.ExecutionFailed,
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            data=event_data,
        ))
        if stop_container:
            self._stop_worker(job.job_id, request=request)
        if preserve_evidence:
            self._preserve_failure_evidence(job, failure=failure, reason=reason)
        if self.lease_store is not None:
            self.lease_store.release_job(job.job_id)

    def _preserve_failure_evidence(
        self,
        job,
        *,
        failure: ExecutionFailure | None = None,
        reason: str | None = None,
    ) -> None:
        """Persist readable evidence and preserve a failed worker workspace.

        Before any destructive cleanup could discard a failed attempt, this
        retains the workspace itself (until plan 5 ships a durable
        candidate/change-set manifest, preserving the workspace beats pretending
        an incomplete patch is enough) and writes a failure-evidence record that
        ties the logs, the task/context identity, and the classified failure to
        the job. It is idempotent and never deletes recoverable work.
        """
        workspace = Path(job.workspace_path)
        if not workspace.is_dir():
            return
        evidence_dir = workspace / ".open-tulid" / "evidence"
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "schema": "tulid.failure-evidence/v1",
                "created_at": _utc_now().isoformat(),
                "job_id": job.job_id,
                "project_id": job.project_id,
                "task_id": job.task_id,
                "transition_id": job.transition_id,
                "worker_id": job.worker_id,
                "preserved_workspace": True,
                "workspace_path": str(workspace),
                "logs_path": str(_agent_log_dir(workspace)),
                "context_path": str(workspace / ".open-tulid" / "job-context.json"),
                "prompt_path": str(workspace / ".open-tulid" / "prompt-packet.md"),
                "failure_reason": reason,
                "failure": failure.to_dict() if failure is not None else None,
            }
            evidence_record_path = evidence_dir / "failure-evidence.json"
            evidence_record_path.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return
        loaded = self.job_store.get(job.job_id)
        if not loaded.accepted or loaded.job is None:
            return
        self.job_store.update_status(
            job.job_id,
            loaded.job.status,
            metadata={
                "failure_evidence_persisted": True,
                "failure_record_path": str(evidence_record_path),
                "preserved_workspace": True,
                "failure_evidence_recorded_at": _utc_now().isoformat(),
            },
        )

    def _stop_worker(self, job_id: str, *, request) -> None:
        stop = getattr(self.containers, "stop_worker_container", None)
        if stop is None:
            return
        try:
            stop(self.runtime.docker_executable, job_id)
        except Exception:
            # Best-effort cleanup; the container may already be gone (docker
            # `--rm`) or stopping may be unsupported by the active backend.
            return

    def _worker_stop_timeout(self) -> float:
        return max(0.1, min(self.runtime.default_timeout_seconds, 30.0))


class _RunLifecycleLivenessProbe:
    """Liveness probe over the blocking worker run.

    Reports alive while the worker process/container run is in flight and not
    alive once it has exited. This reflects actual process/container liveness
    (the foreground ``docker run`` for a container worker), never GPU usage and
    never a stored job status.
    """

    __slots__ = ("_holder",)

    def __init__(self, holder: list[threading.Thread | None]) -> None:
        self._holder = holder

    def is_alive(self) -> bool:
        thread = self._holder[0]
        if thread is None or thread.ident is None:
            # Registered before/around process start but not yet begun. The
            # worker is still expected to be running, so report alive.
            return True
        return thread.is_alive()


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
) -> str | None:
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
) -> str | None:
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


def _updated_attempt_record(
    record: AttemptRecord,
    *,
    status: str,
    ended_at: str | None = None,
    failure_reference: str | None = None,
) -> AttemptRecord:
    return _replace_request(
        record,
        status=status,
        ended_at=ended_at or record.ended_at,
        failure_reference=failure_reference or record.failure_reference,
    )


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


def _write_prompt_manifest(workspace: Path, compiled: CompiledPrompt) -> None:
    context_dir = workspace / ".open-tulid"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "prompt-manifest.json").write_text(
        json.dumps(compiled.manifest.to_dict(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_prompt_manifest_payload(workspace: Path, manifest: Mapping[str, object]) -> None:
    context_dir = workspace / ".open-tulid"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "prompt-manifest.json").write_text(
        json.dumps(dict(manifest), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _frozen_prompt_packet(job, contract: ExecutionContract | None) -> str | None:
    if contract is None:
        return None
    if (
        not isinstance(job.metadata.get("prompt_packet"), str)
        or not isinstance(job.metadata.get("prompt_packet_sha256"), str)
    ):
        raise ValueError("frozen execution job has no immutable prompt packet")
    compiled = compiled_prompt_from_metadata(job.metadata)
    if compiled.manifest.execution_contract_sha256 != contract.sha256:
        raise ValueError("frozen prompt manifest does not match the execution contract")
    return compiled.text


def _repair_prompt_packet(job) -> str:
    packet = job.metadata.get("repair_packet")
    if not isinstance(packet, str) or not packet.strip():
        raise ValueError("repair-ready job has no repair packet")
    return packet


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
    derived_artifact_type: str | None,
) -> str:
    variant = _runtime_prompt_variant(
        transition_id=transition_id,
        required_artifacts=required_artifacts,
        derived_artifact_type=derived_artifact_type,
    )
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
        "## Task Body",
        task_body.strip(),
        "",
        _render_prompt_priority_section(variant),
        "",
        _render_prompt_paths_section(variant, required_artifacts=required_artifacts),
        "",
        _render_validation_failure_policy_section(variant),
    ]
    return "\n".join(sections)


def _append_completion_submission(
    prompt_text: str,
    *,
    required_artifacts: tuple[str, ...],
    required_validations: tuple[str, ...],
    required_validation_details: tuple[str, ...],
    changed_files_required: bool,
    derived_artifact_type: str | None,
    derived_artifact_required: bool = True,
    parent_to_if_derived: str | None = None,
) -> str:
    artifact_labels = list(required_artifacts)
    if derived_artifact_type is not None:
        derivation_label = f"{derived_artifact_type} (derived"
        if not derived_artifact_required:
            derivation_label += ", optional"
        artifact_labels.append(f"{derivation_label})")
    artifacts = ", ".join(artifact_labels) if artifact_labels else "none"
    validations = ", ".join(required_validations) if required_validations else "none"
    completion = _render_prompt_completion_section(
        required_artifacts=required_artifacts,
        required_validations=required_validations,
        required_validation_details=required_validation_details,
        changed_files_required=changed_files_required,
        derived_artifact_type=derived_artifact_type,
        derived_artifact_required=derived_artifact_required,
        parent_to_if_derived=parent_to_if_derived,
        artifacts=artifacts,
        validations=validations,
    )
    return f"{prompt_text.rstrip()}\n\n{completion}"


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
            "Complete the user-requested outcome defined by the current task and its generated execution contract when present.",
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
            "1. The current task is authoritative for the user-requested outcome.",
            "2. A generated execution contract, when present, is binding for the resolved objective, change surface, interfaces, requirements, and checks; it may refine but never broaden that outcome.",
            "3. Required validations and completion requirements are mandatory.",
            "4. Other parent and linked context is background reference material only.",
            "5. If reference material suggests broader project work, stay within the task and contract instead of expanding scope.",
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
    required_artifacts: tuple[str, ...],
    required_validations: tuple[str, ...],
    required_validation_details: tuple[str, ...],
    changed_files_required: bool,
    derived_artifact_type: str | None,
    derived_artifact_required: bool,
    parent_to_if_derived: str | None,
    artifacts: str,
    validations: str,
) -> str:
    lines = [
        "## Completion Submission",
        f"Required artifacts: {artifacts}",
        f"Required validations: {validations}",
    ]
    if required_artifacts:
        lines.append("Only create the artifact files explicitly required for this transition.")
    elif derived_artifact_type:
        lines.append("No fixed artifact is required; follow the derived-task artifact rules below.")
    else:
        lines.extend((
            "No artifacts are required for this transition. Submit an empty `artifacts` array.",
            "Treat existing files under `output/` as read-only context unless this transition explicitly requires output artifacts.",
            "Do not regenerate product specs, technical directions, implementation specs, or task breakdown files for an implementation transition.",
        ))
    if derived_artifact_type:
        if derived_artifact_required:
            lines.extend((
                f"This transition derives child tasks via `{derived_artifact_type}` artifacts.",
                f"Submit one artifact entry per generated `{derived_artifact_type}` file.",
                "At least one derived-task artifact is required.",
            ))
        else:
            lines.extend((
                f"This transition may derive child tasks via `{derived_artifact_type}` artifacts.",
                f"Submit a `{derived_artifact_type}` artifact only when a child task is needed.",
                "If no child task is needed, do not create or submit this optional artifact.",
            ))
            if parent_to_if_derived:
                lines.append(
                    f"Submitting one or more `{derived_artifact_type}` artifacts routes the parent to "
                    f"`{parent_to_if_derived}`; submitting none uses the transition's normal destination."
                )
        lines.extend((
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
        "When the work and required validations are complete, submit completion evidence with `curl`.",
        "Do not exit successfully until this request has been made and accepted.",
        "If `curl` is unavailable or the request fails, treat that as a blocking runtime error.",
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
        _completion_artifacts_line(
            required_artifacts,
            derived_artifact_type=derived_artifact_type,
            derived_artifact_required=derived_artifact_required,
        ),
        "    \"changed_files\": [\"relative/workspace/path\"],",
        _completion_validation_evidence_line(required_validations),
        "}",
        "JSON",
        "```",
        "",
        "If completion is rejected, use the returned errors as feedback, fix the workspace, and submit again.",
        "If completion is still in progress, remain active and wait for the final completion response instead of exiting.",
        "A zero exit code without an accepted completion submission is a failed Tulid job.",
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


def _completion_artifacts_line(
    required_artifacts: tuple[str, ...],
    *,
    derived_artifact_type: str | None,
    derived_artifact_required: bool,
) -> str:
    artifact_types = list(required_artifacts)
    if derived_artifact_type is not None and derived_artifact_required:
        artifact_types.append(derived_artifact_type)
    if not artifact_types:
        return '    "artifacts": [],'
    artifact_examples = ", ".join(
        f'{{"type": "{artifact_type}", "path": "relative/path/in/output"}}'
        for artifact_type in artifact_types
    )
    return f'    "artifacts": [{artifact_examples}],'


def _completion_validation_evidence_line(required_validations: tuple[str, ...]) -> str:
    if not required_validations:
        return '    "validation_evidence": {}'
    entries = ", ".join(
        f'"{validation}": "command/result evidence"'
        for validation in required_validations
    )
    return f'    "validation_evidence": {{{entries}}}'


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
    opencode_config_home: str | None = None,
) -> str | None:
    worker_type = runtime.worker_types.get(worker_id, runtime.worker_types.get(implementation_id, implementation_id))
    if worker_type != "opencode" and implementation_id != "opencode":
        return None
    endpoint = env.get("OPEN_TULID_MODEL_ENDPOINT")
    proxy_id = env.get("OPEN_TULID_MODEL_PROXY_ID")
    if not endpoint or not proxy_id or "OPEN_TULID_MODEL_SESSION_TOKEN" not in env:
        return None
    model_ref = _model_arg(args)
    if model_ref is None or "/" not in model_ref:
        return None
    provider_id, model_id = model_ref.split("/", 1)
    expected_provider_id = f"tulid-{proxy_id}"
    if provider_id != expected_provider_id or not model_id:
        return None

    # This is Tulid runtime configuration, not implementation output.  Keep it
    # beneath the verifier-excluded runtime directory so it cannot appear as a
    # contract-breaking repository change.
    install_home_rel = (opencode_config_home or ".open-tulid/home").lstrip("/").rstrip("/")
    install_home = workspace / install_home_rel
    path = install_home / ".config" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
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
    return f"{runtime.container_workspace}/{install_home_rel}/.config/opencode/opencode.json"


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


def _job_status_str(result) -> str:
    if not result.accepted or result.job is None:
        return ""
    status = result.job.status
    return status.value if hasattr(status, "value") else str(status)


def _adapter_project_root(adapter: StorageAdapter) -> Path | None:
    config = getattr(adapter, "config", None)
    project_root = getattr(config, "project_root", None)
    return project_root if isinstance(project_root, Path) else None


def _planning_inputs_text(
    adapter: StorageAdapter,
    project_root: Path | None,
    transition: TransitionDefinition,
    task: Task,
    parent_tasks: tuple[Task, ...],
) -> str:
    """Render repository facts and the unfinished task set for planning workers.

    Only specification and breakdown transitions receive these inputs. Both are
    derived from the live adapter/project root at render time, which is already
    how the legacy planning prompt resolves its linked context.
    """
    from open_tulid.runtime.planning_input import build_planning_inputs, requires_planning_inputs

    if project_root is None or not requires_planning_inputs(transition):
        return ""
    from open_tulid.runtime.repository_facts import capture_repository_snapshot

    repository = capture_repository_snapshot(project_root)
    project = adapter.load_project()
    return build_planning_inputs(
        repository=repository,
        project=project,
        current_task=task,
        parent_tasks=parent_tasks,
    ).text


def _project_standard_runtime(adapter: StorageAdapter) -> object:
    """Resolve the project standard contract's runtime settings, if configured."""
    from .standard_contracts import load_standard_contract

    project_root = _adapter_project_root(adapter)
    if project_root is None:
        return _StandardRuntimeDiscovery(None, ".open-tulid/home")
    loaded = load_standard_contract(project_root)
    if not loaded.accepted or loaded.contract is None:
        return _StandardRuntimeDiscovery(None, ".open-tulid/home")
    contract = loaded.contract
    return _StandardRuntimeDiscovery(
        container_user=contract.runtime.container_user,
        opencode_config_home=contract.runtime.opencode_config_home,
    )


class _StandardRuntimeDiscovery:
    __slots__ = ("container_user", "opencode_config_home")

    def __init__(self, container_user: str | None, opencode_config_home: str) -> None:
        self.container_user = container_user
        self.opencode_config_home = opencode_config_home


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


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
