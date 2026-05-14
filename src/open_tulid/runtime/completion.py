from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import DomainError, EventActor, EventType, ExecutionJobStatus, Task, WorkflowDefinition
from open_tulid.runtime.events import JsonlEventStore, build_event, new_ulid
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.transactions import FileTransactionRuntime
from open_tulid.runtime.events import TransactionJournalStore

from .verifier import (
    ArtifactSubmission,
    CompletionSubmission,
    DeterministicVerifier,
    VerificationResult,
    normalize_artifacts,
)


TERMINAL_JOB_STATUSES = frozenset({
    ExecutionJobStatus.ACCEPTED.value,
    ExecutionJobStatus.FAILED.value,
    ExecutionJobStatus.STALE.value,
    ExecutionJobStatus.CANCELLED.value,
})


@dataclass(frozen=True)
class CompletionResult:
    accepted: bool
    verification: VerificationResult | None = None
    errors: tuple[DomainError, ...] = ()


@dataclass(frozen=True)
class _EffectApplyResult:
    accepted: bool
    message: str = ""
    errors: tuple[DomainError, ...] = ()


class CompletionService:
    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        adapter: StorageAdapter,
        job_store: FileExecutionJobStore,
        event_store: JsonlEventStore,
        journal_store: TransactionJournalStore | None = None,
        artifact_root: Path | None = None,
        verifier: DeterministicVerifier | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.event_store = event_store
        self.journal_store = journal_store
        self.artifact_root = artifact_root
        self.verifier = verifier or DeterministicVerifier(
            artifact_templates={
                artifact_id: artifact.template
                for artifact_id, artifact in workflow.artifact_types.items()
            },
        )

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
        submission_id = submission.submission_id or new_ulid()
        prior_submission = _prior_submission(job.metadata, submission_id)
        if prior_submission is not None:
            if prior_submission.get("accepted") is True:
                return CompletionResult(True)
            return CompletionResult(False, errors=tuple(
                DomainError(
                    code=str(error.get("code", "completion.replayed_rejection")),
                    message=str(error.get("message", "Completion submission was already rejected.")),
                    location=error.get("location") if isinstance(error.get("location"), str) else None,
                )
                for error in prior_submission.get("feedback", ())
                if isinstance(error, Mapping)
            ) or (_error("completion.replayed_rejection", "Completion submission was already rejected."),))

        if _status(job.status) in TERMINAL_JOB_STATUSES:
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="executor", id=job.worker_id),
                event_type="ExecutionCompletionConflict",
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"reason": "terminal_job", "status": _status(job.status)},
            ))
            return CompletionResult(False, errors=(_error(
                "completion.job_terminal",
                f"Execution job {job.job_id!r} is terminal: {_status(job.status)}.",
                job.job_id,
            ),))

        expected_token = job.metadata.get("completion_token")
        if expected_token is not None and token != expected_token:
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="executor", id=job.worker_id),
                event_type="ExecutionCompletionForbidden",
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"reason": "identity_mismatch"},
            ))
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
                "attempt": submission.attempt,
                "artifacts": [_artifact_to_dict(artifact) for artifact in submission.artifacts],
                "changed_files": list(submission.changed_files),
                "validation_evidence": dict(submission.validation_evidence),
            },
        ))

        verification = self.verifier.verify(
            workspace=Path(job.workspace_path),
            output_dir=Path(str(job.metadata.get("output_path", Path(job.workspace_path) / "output"))),
            transition=transition,
            submission=submission,
        )
        if not verification.accepted:
            self.job_store.update_status(
                job.job_id,
                ExecutionJobStatus.COMPLETION_REJECTED,
                metadata={
                    "last_verification": verification.message,
                    "completion_submissions": _record_submission(
                        job.metadata,
                        submission_id,
                        accepted=False,
                        feedback=tuple(_error_to_dict(error) for error in verification.errors),
                    ),
                },
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
                data={"feedback": [_error_to_dict(error) for error in verification.errors]},
            ))
            return CompletionResult(False, verification=verification, errors=verification.errors)

        output_dir = Path(str(job.metadata.get("output_path", Path(job.workspace_path) / "output")))
        promoted_artifacts = _promotion_plan(
            artifact_root=self.artifact_root,
            output_dir=output_dir,
            task_id=job.task_id,
            artifacts=normalize_artifacts(submission.artifacts),
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
                data={"to_state": transition.to_state},
            ),
            *(
                build_event(
                    project_id=job.project_id,
                    actor=EventActor(type="system", id="task-manager-runtime"),
                    event_type=EventType.ArtifactWritten,
                    correlation_id=job.job_id,
                    task_id=job.task_id,
                    job_id=job.job_id,
                    transition_id=job.transition_id,
                    submission_id=submission_id,
                    data={
                        "artifact_type": item["artifact_type"],
                        "source_path": item["source_path"],
                        "target_path": item["target_path"],
                    },
                )
                for item in promoted_artifacts
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
        effects = (
            *(
                {
                    "type": "promote_artifact",
                    "task_id": job.task_id,
                    "source_path": item["source_path"],
                    "target_path": item["target_path"],
                    "link": item["link"],
                }
                for item in promoted_artifacts
            ),
            {"type": "move_task", "task_id": job.task_id, "to_state": transition.to_state},
        )
        transaction = self._apply_acceptance(
            project_id=job.project_id,
            task_id=job.task_id,
            transition_id=job.transition_id,
            effects=effects,
            events=events,
        )
        if not transaction.accepted:
            self.job_store.update_status(
                job.job_id,
                ExecutionJobStatus.FAILED,
                metadata={"completion_error": transaction.message},
            )
            return CompletionResult(False, verification=verification, errors=transaction.errors)

        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.ACCEPTED,
            metadata={
                "completed_submission_id": submission_id,
                "promoted_artifacts": tuple(promoted_artifacts),
                "completion_submissions": _record_submission(
                    job.metadata,
                    submission_id,
                    accepted=True,
                    feedback=(),
                ),
            },
        )
        return CompletionResult(True, verification=verification)

    def _apply_acceptance(
        self,
        *,
        project_id: str,
        task_id: str,
        transition_id: str,
        effects: tuple[Mapping[str, object], ...],
        events: tuple[object, ...],
    ) -> _EffectApplyResult:
        if self.journal_store is None:
            for effect in effects:
                result = self._apply_effect(effect)
                if not result.accepted:
                    return result
            appended = self.event_store.append_many(events)
            if not appended.accepted:
                return _EffectApplyResult(False, "event append failed", (appended.error or _error("event.append_failed", "Event append failed."),))
            return _EffectApplyResult(True)

        runtime = FileTransactionRuntime(
            journals=self.journal_store,
            events=self.event_store,
            apply_effect=self._apply_effect,
        )
        applied = runtime.apply(
            project_id=project_id,
            task_id=task_id,
            transition_id=transition_id,
            effects=effects,
            events=events,
        )
        if not applied.accepted:
            error = applied.error or _error("transaction.failed", "Completion transaction failed.")
            return _EffectApplyResult(False, error.message, (error,))
        return _EffectApplyResult(True)

    def _apply_effect(self, effect: Mapping[str, object]) -> _EffectApplyResult:
        effect_type = effect.get("type")
        if effect_type == "move_task":
            task_id = str(effect.get("task_id", ""))
            to_state = str(effect.get("to_state", ""))
            moved = self.adapter.move_task(task_id, to_state)
            return _EffectApplyResult(moved.accepted, "task moved", moved.errors)
        if effect_type == "promote_artifact":
            source_path = Path(str(effect.get("source_path", "")))
            target_path = Path(str(effect.get("target_path", "")))
            link = str(effect.get("link", ""))
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
            except OSError as exc:
                return _EffectApplyResult(False, f"artifact promotion failed: {exc}", (_error(
                    "artifact.promotion_failed",
                    f"Cannot promote artifact: {exc}",
                    str(target_path),
                ),))
            if link:
                loaded = self.adapter.read_task(str(effect.get("task_id", "")))
                if loaded.accepted and loaded.task is not None:
                    links = tuple(dict.fromkeys((*loaded.task.artifact_links, link)))
                    updated = _task_with_links(loaded.task, links)
                    written = self.adapter.write_task(updated)
                    if not written.accepted:
                        return _EffectApplyResult(False, "artifact link update failed", written.errors)
            return _EffectApplyResult(True)
        return _EffectApplyResult(False, f"unknown effect: {effect_type}", (_error(
            "effect.unknown",
            f"Unknown completion effect: {effect_type}",
        ),))


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _error_to_dict(error: DomainError) -> Mapping[str, object]:
    return {"code": error.code, "message": error.message, "location": error.location}


def _promotion_plan(
    *,
    artifact_root: Path | None,
    output_dir: Path,
    task_id: str,
    artifacts: tuple[ArtifactSubmission, ...],
) -> tuple[Mapping[str, str], ...]:
    if artifact_root is None:
        return ()
    planned: list[Mapping[str, str]] = []
    for artifact in artifacts:
        source = output_dir / artifact.path
        file_name = Path(artifact.path).name
        target = artifact_root / task_id / artifact.type / file_name
        planned.append({
            "artifact_type": artifact.type,
            "source_path": str(source),
            "target_path": str(target),
            "link": _artifact_link(artifact_root, target),
        })
    return tuple(planned)


def _artifact_link(artifact_root: Path, target: Path) -> str:
    try:
        return str(target.relative_to(artifact_root.parent))
    except ValueError:
        return str(target)


def _task_with_links(task: Task, links: tuple[str, ...]) -> Task:
    return Task(
        id=task.id,
        title=task.title,
        path=task.path,
        current_state=task.current_state,
        task_type=task.task_type,
        dependencies=task.dependencies,
        artifact_links=links,
        parent_id=task.parent_id,
        metadata=task.metadata,
        body=task.body,
    )


def _artifact_to_dict(artifact: ArtifactSubmission | str) -> Mapping[str, object]:
    if not isinstance(artifact, ArtifactSubmission):
        clean = str(artifact)
        return {"type": clean, "path": clean}
    payload: dict[str, object] = {"type": artifact.type, "path": artifact.path}
    if artifact.sha256 is not None:
        payload["sha256"] = artifact.sha256
    return payload


def _status(status: ExecutionJobStatus | str) -> str:
    return status.value if isinstance(status, ExecutionJobStatus) else str(status)


def _prior_submission(metadata: Mapping[str, object], submission_id: str) -> Mapping[str, object] | None:
    submissions = metadata.get("completion_submissions", {})
    if not isinstance(submissions, Mapping):
        return None
    prior = submissions.get(submission_id)
    return prior if isinstance(prior, Mapping) else None


def _record_submission(
    metadata: Mapping[str, object],
    submission_id: str,
    *,
    accepted: bool,
    feedback: tuple[Mapping[str, object], ...],
) -> Mapping[str, object]:
    submissions = metadata.get("completion_submissions", {})
    if not isinstance(submissions, Mapping):
        submissions = {}
    return {
        **dict(submissions),
        submission_id: {
            "accepted": accepted,
            "feedback": tuple(dict(item) for item in feedback),
        },
    }
