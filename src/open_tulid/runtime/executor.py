from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from open_tulid.adapters.base import StorageAdapter
from open_tulid.containers.runtime import AgentRunResult, request_for_worker, run_agent_container
from open_tulid.domain import DomainError, EventActor, EventType, ExecutionJobStatus, WorkflowDefinition
from open_tulid.models import ProjectConfig, RuntimeConfig
from open_tulid.runtime.events import JsonlEventStore, build_event
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.workspaces import WorkspacePreparer


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
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.event_store = event_store
        self.runtime = runtime
        self.project_config = project_config

    def run(self, job_id: str) -> ExecutorRunResult:
        loaded = self.job_store.get(job_id)
        if not loaded.accepted or loaded.job is None:
            return ExecutorRunResult(False, errors=(loaded.error or _error("job.not_found", "Job was not found."),))
        job = loaded.job
        transition = self.workflow.transitions.get(job.transition_id)
        if transition is None:
            return ExecutorRunResult(False, errors=(_error("transition.not_found", "Transition was not found."),))
        task_result = self.adapter.read_task(job.task_id)
        if not task_result.accepted or task_result.task is None:
            return ExecutorRunResult(False, errors=task_result.errors or (_error("task.not_found", "Task was not found."),))

        prepared = WorkspacePreparer(repo_root=self.project_config.repo_root).prepare(
            job=job,
            task=task_result.task,
            transition=transition,
            completion_endpoint=f"/jobs/{job.job_id}/complete",
        )
        if not prepared.accepted or prepared.workspace is None:
            return ExecutorRunResult(False, errors=(prepared.error or _error("workspace.prepare_failed", "Workspace failed."),))

        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.RUNNING,
            metadata={"workspace_prepared": True},
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
            worker_id=job.worker_id,
            workspace=prepared.workspace,
            runtime=self.runtime,
            env={
                "OPEN_TULID_JOB_ID": job.job_id,
                "OPEN_TULID_COMPLETION_TOKEN": str(job.metadata.get("completion_token", "")),
            },
        )
        result = run_agent_container(request, docker_executable=self.runtime.docker_executable)
        _write_run_logs(Path(job.workspace_path), result)
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
        return ExecutorRunResult(True, run=result)


def _write_run_logs(workspace: Path, result: AgentRunResult) -> None:
    log_dir = workspace / ".open-tulid" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (log_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    (log_dir / "command.txt").write_text(" ".join(result.command) + "\n", encoding="utf-8")


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
