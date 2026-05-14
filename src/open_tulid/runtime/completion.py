from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import DomainError, EventActor, EventType, ExecutionJobStatus, WorkflowDefinition
from open_tulid.runtime.events import JsonlEventStore, build_event, new_ulid
from open_tulid.runtime.jobs import FileExecutionJobStore

from .verifier import CompletionSubmission, DeterministicVerifier, VerificationResult


@dataclass(frozen=True)
class CompletionResult:
    accepted: bool
    verification: VerificationResult | None = None
    errors: tuple[DomainError, ...] = ()


class CompletionService:
    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        adapter: StorageAdapter,
        job_store: FileExecutionJobStore,
        event_store: JsonlEventStore,
        verifier: DeterministicVerifier | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.event_store = event_store
        self.verifier = verifier or DeterministicVerifier()

    def submit(
        self,
        *,
        job_id: str,
        submission: CompletionSubmission,
        token: str | None = None,
    ) -> CompletionResult:
        loaded = self.job_store.get(job_id)
        if not loaded.accepted or loaded.job is None:
            return CompletionResult(False, errors=(loaded.error or _error("job.not_found", "Job was not found."),))
        job = loaded.job

        expected_token = job.metadata.get("completion_token")
        if expected_token is not None and token != expected_token:
            return CompletionResult(False, errors=(_error(
                "completion.identity_mismatch",
                "Completion token does not match the job context.",
                job_id,
            ),))

        transition = self.workflow.transitions.get(job.transition_id)
        if transition is None:
            return CompletionResult(False, errors=(_error(
                "transition.not_found",
                f"Transition {job.transition_id!r} is not defined.",
                job.transition_id,
            ),))

        submission_id = new_ulid()
        actor = EventActor(type="executor", id=job.worker_id)
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=actor,
            event_type=EventType.ExecutionCompletionSubmitted,
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            submission_id=submission_id,
            data={
                "summary": submission.summary,
                "artifacts": list(submission.artifacts),
                "changed_files": list(submission.changed_files),
                "validation_evidence": dict(submission.validation_evidence),
            },
        ))

        verification = self.verifier.verify(
            workspace=Path(job.workspace_path),
            transition=transition,
            submission=submission,
        )
        if not verification.accepted:
            self.job_store.update_status(
                job.job_id,
                ExecutionJobStatus.RUNNING,
                metadata={"last_verification": verification.message},
            )
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="completion-verifier"),
                event_type=EventType.ExecutionCompletionRejected,
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"errors": [_error_to_dict(error) for error in verification.errors]},
            ))
            return CompletionResult(False, verification=verification, errors=verification.errors)

        moved = self.adapter.move_task(job.task_id, transition.to_state)
        if not moved.accepted:
            self.job_store.update_status(
                job.job_id,
                ExecutionJobStatus.FAILED,
                metadata={"completion_error": "task move failed"},
            )
            return CompletionResult(False, verification=verification, errors=moved.errors)

        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.COMPLETED,
            metadata={"completed_submission_id": submission_id},
        )
        events = (
            build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="completion-verifier"),
                event_type=EventType.TransitionAccepted,
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"from_state": transition.from_state, "to_state": transition.to_state},
            ),
            build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="task-manager-runtime"),
                event_type=EventType.TaskMoved,
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"to_state": transition.to_state, "path": moved.path},
            ),
            build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="task-manager-runtime"),
                event_type=EventType.ReviewRequested,
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"summary": submission.summary},
            ),
            build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="task-manager-runtime"),
                event_type=EventType.ExecutionFinished,
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"accepted": True},
            ),
        )
        self.event_store.append_many(events)
        return CompletionResult(True, verification=verification)


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _error_to_dict(error: DomainError) -> Mapping[str, object]:
    return {"code": error.code, "message": error.message, "location": error.location}
