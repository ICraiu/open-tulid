from __future__ import annotations

import hashlib
import shutil
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from io import StringIO

from ruamel.yaml import YAML

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import DomainError, EventActor, EventType, ExecutionJobStatus, Task, WorkflowDefinition
from open_tulid.runtime.events import JsonlEventStore, build_event, new_ulid, utc_now
from open_tulid.runtime.execution_contracts import load_job_execution_contract
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
from .repairs import DEFAULT_MAX_REPAIR_ATTEMPTS, plan_repair


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
        repo_root: Path | None = None,
        repo_command_runner: object | None = None,
        verifier: DeterministicVerifier | None = None,
        validation_implementations: Mapping[str, object] | None = None,
        validation_context_factory: object | None = None,
        max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.event_store = event_store
        self.journal_store = journal_store
        self.artifact_root = artifact_root
        self.repo_root = repo_root
        self.repo_command_runner = repo_command_runner
        self.verifier = verifier or DeterministicVerifier(
            artifact_templates={
                artifact_id: artifact.template
                for artifact_id, artifact in workflow.artifact_types.items()
            },
            validation_implementations=validation_implementations,
            validation_context_factory=validation_context_factory,
        )
        self.max_repair_attempts = max_repair_attempts

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

        if _status(job.status) == ExecutionJobStatus.ACCEPTED.value:
            return CompletionResult(True)

        if _status(job.status) == ExecutionJobStatus.COMPLETION_SUBMITTED.value:
            return CompletionResult(False, errors=(_error(
                "completion.in_progress",
                (
                    f"Execution job {job.job_id!r} already has a completion being validated. "
                    "Remain active and wait for final completion feedback; do not exit successfully yet."
                ),
                job.job_id,
            ),))

        if _status(job.status) in TERMINAL_JOB_STATUSES:
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="executor", id=job.worker_id),
                event_type="ExecutionCompletionIgnored",
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

        frozen = load_job_execution_contract(job)
        if not frozen.accepted:
            return CompletionResult(False, errors=frozen.errors)
        transition = (
            frozen.contract.transition
            if frozen.contract is not None
            else self.workflow.transitions.get(job.transition_id)
        )
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
        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.COMPLETION_SUBMITTED,
            metadata={
                "active_submission_id": submission_id,
                "completion_submitted_at": utc_now(),
                "completion_validation_started_at": utc_now(),
            },
        )

        validation_started = time.monotonic()
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=EventActor(type="system", id="completion-verifier"),
            event_type="ExecutionCompletionValidationStarted",
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            submission_id=submission_id,
            data={
                "validations": tuple(call.type for call in transition.requires.validations),
                "changed_files": list(submission.changed_files),
                "artifact_count": len(submission.artifacts),
            },
        ))
        try:
            verification = self.verifier.verify(
                workspace=Path(job.workspace_path),
                output_dir=Path(str(job.metadata.get("output_path", Path(job.workspace_path) / "output"))),
                transition=transition,
                submission=submission,
                execution_contract=frozen.contract,
            )
        except Exception as exc:
            duration_seconds = round(time.monotonic() - validation_started, 3)
            self._record_validation_finished(
                job=job,
                submission_id=submission_id,
                accepted=False,
                duration_seconds=duration_seconds,
                error_codes=("completion.validation_exception",),
                error_count=1,
                detail=str(exc),
            )
            raise
        duration_seconds = round(time.monotonic() - validation_started, 3)
        self._record_validation_finished(
            job=job,
            submission_id=submission_id,
            accepted=verification.accepted,
            duration_seconds=duration_seconds,
            error_codes=tuple(error.code for error in verification.errors),
            error_count=len(verification.errors),
            verification_report=(verification.report.to_dict() if verification.report is not None else None),
        )
        if not verification.accepted:
            return self._reject_completion(
                job=job,
                submission_id=submission_id,
                verification=verification,
                errors=verification.errors,
                message=verification.message,
            )

        output_dir = Path(str(job.metadata.get("output_path", Path(job.workspace_path) / "output")))
        promoted_artifacts = _promotion_plan(
            artifact_root=self.artifact_root,
            output_dir=output_dir,
            task_id=job.task_id,
            artifacts=normalize_artifacts(submission.artifacts),
            existing_task=self.adapter.read_task(job.task_id).task,
        )
        promoted_files = _changed_file_plan(
            repo_root=self.repo_root,
            workspace=Path(job.workspace_path),
            changed_files=submission.changed_files,
        )
        commit_effect = _commit_plan(
            repo_root=self.repo_root,
            changed_files=promoted_files,
            task=self.adapter.read_task(job.task_id).task,
        )
        existing_task_ids, existing_task_errors = self._existing_task_ids(job.task_id) if transition.derives is not None else ((), ())
        if existing_task_errors:
            return self._reject_completion(
                job=job,
                submission_id=submission_id,
                verification=verification,
                errors=existing_task_errors,
                message=_format_errors(existing_task_errors),
            )
        derived_tasks, derivation_errors = _derived_task_plan(
            output_dir=output_dir,
            transition=transition,
            artifacts=normalize_artifacts(submission.artifacts),
            parent_id=job.task_id,
            existing_task_ids=existing_task_ids,
        )
        if derivation_errors:
            return self._reject_completion(
                job=job,
                submission_id=submission_id,
                verification=verification,
                errors=derivation_errors,
                message=_format_errors(derivation_errors),
            )
        effective_to_state = transition.to_state
        if (
            derived_tasks
            and transition.derives is not None
            and transition.derives.parent_to_if_derived is not None
        ):
            effective_to_state = transition.derives.parent_to_if_derived
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
                data={"from_state": transition.from_state, "to_state": effective_to_state},
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
                data={
                    "from_state": transition.from_state,
                    "to_state": effective_to_state,
                    "reason": "completion_accepted",
                },
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
            *(
                build_event(
                    project_id=job.project_id,
                    actor=EventActor(type="system", id="task-manager-runtime"),
                    event_type=EventType.TaskDerived,
                    correlation_id=job.job_id,
                    task_id=item["task"].id,
                    job_id=job.job_id,
                    transition_id=job.transition_id,
                    submission_id=submission_id,
                    data={
                        "parent_id": job.task_id,
                        "state": item["task"].current_state,
                        "task_type": item["task"].task_type,
                    },
                )
                for item in derived_tasks
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
                    "type": "promote_changed_file",
                    "source_path": item["source_path"],
                    "target_path": item["target_path"],
                }
                for item in promoted_files
            ),
            *((commit_effect,) if commit_effect is not None else ()),
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
            *(
                {
                    "type": "create_task",
                    "task": _task_to_dict(item["task"]),
                }
                for item in derived_tasks
            ),
            *(
                ({
                    "type": "link_derived_tasks",
                    "parent_id": job.task_id,
                    "child_links": tuple(item["link"] for item in derived_tasks),
                },) if derived_tasks else ()
            ),
            {"type": "move_task", "task_id": job.task_id, "to_state": effective_to_state},
        )
        transaction = self._apply_acceptance(
            project_id=job.project_id,
            task_id=job.task_id,
            transition_id=job.transition_id,
            expected_to_state=effective_to_state,
            effects=effects,
            events=events,
        )
        if not transaction.accepted:
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="system", id="task-manager-runtime"),
                event_type="ExecutionCompletionAcceptanceFailed",
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={
                    "message": transaction.message,
                    "error_codes": tuple(error.code for error in transaction.errors),
                },
            ))
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
                "promoted_files": tuple(promoted_files),
                "completion_submissions": _record_submission(
                    job.metadata,
                    submission_id,
                    accepted=True,
                    feedback=(),
                ),
            },
        )
        return CompletionResult(True, verification=verification)

    def _reject_completion(
        self,
        *,
        job,
        submission_id: str,
        verification: VerificationResult,
        errors: tuple[DomainError, ...],
        message: str,
    ) -> CompletionResult:
        current = self.job_store.get(job.job_id)
        current_job = current.job if current.accepted else None
        if current_job is not None and _status(current_job.status) in TERMINAL_JOB_STATUSES:
            self.event_store.append(build_event(
                project_id=job.project_id,
                actor=EventActor(type="executor", id=job.worker_id),
                event_type="ExecutionCompletionIgnored",
                correlation_id=job.job_id,
                task_id=job.task_id,
                job_id=job.job_id,
                transition_id=job.transition_id,
                submission_id=submission_id,
                data={"reason": "terminal_job", "status": _status(current_job.status)},
            ))
            return CompletionResult(False, verification=verification, errors=(_error(
                "completion.job_terminal",
                f"Execution job {job.job_id!r} is terminal: {_status(current_job.status)}.",
                job.job_id,
            ),))
        metadata = current_job.metadata if current_job is not None else job.metadata
        report = verification.report
        repair = plan_repair(
            report=report,
            errors=errors,
            repair_attempts=int(metadata.get("repair_attempts", 0)),
            max_repair_attempts=self.max_repair_attempts,
        )
        repair_history = list(metadata.get("repair_history", ()))
        repair_history.append({
            "submission_id": submission_id,
            "classification": report.classification if report is not None else None,
            "verification_report": report.to_dict() if report is not None else None,
            "error_codes": [error.code for error in errors],
            "repair_ready": repair.eligible,
        })
        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.COMPLETION_REJECTED,
            metadata={
                "last_verification": message,
                "repair_ready": repair.eligible,
                "repair_packet": repair.packet,
                "repair_blocked_reason": repair.reason,
                "repair_history": tuple(repair_history),
                "completion_submissions": _record_submission(
                    metadata,
                    submission_id,
                    accepted=False,
                    feedback=tuple(_error_to_dict(error) for error in errors),
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
            data={"feedback": [_error_to_dict(error) for error in errors]},
        ))
        return CompletionResult(False, verification=verification, errors=errors)

    def _record_validation_finished(
        self,
        *,
        job,
        submission_id: str,
        accepted: bool,
        duration_seconds: float,
        error_codes: tuple[str, ...],
        error_count: int,
        detail: str | None = None,
        verification_report: Mapping[str, object] | None = None,
    ) -> None:
        data = {
            "accepted": accepted,
            "duration_seconds": duration_seconds,
            "error_codes": list(error_codes),
            "error_count": error_count,
        }
        if detail is not None:
            data["detail"] = detail
        if verification_report is not None:
            data["verification_report"] = dict(verification_report)
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=EventActor(type="system", id="completion-verifier"),
            event_type="ExecutionCompletionValidationFinished",
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            submission_id=submission_id,
            data=data,
        ))
        current = self.job_store.get(job.job_id)
        if not current.accepted or current.job is None:
            return
        self.job_store.update_status(
            job.job_id,
            current.job.status,
            metadata={
                "completion_validation_finished_at": utc_now(),
                "completion_validation_duration_seconds": duration_seconds,
                "completion_validation_error_codes": tuple(error_codes),
                "completion_validation_error_count": error_count,
                **({"verification_report": dict(verification_report)} if verification_report is not None else {}),
            },
        )

    def _existing_task_ids(self, task_id: str) -> tuple[tuple[str, ...], tuple[DomainError, ...]]:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return (), loaded.errors
        if loaded.snapshot is None:
            return (), (_error("project.snapshot_missing", "Adapter returned no project snapshot.", task_id),)
        return tuple(loaded.snapshot.tasks.keys()), ()

    def _apply_acceptance(
        self,
        *,
        project_id: str,
        task_id: str,
        transition_id: str,
        expected_to_state: str,
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
            compensate_effect=self._compensate_effect,
            validate_final_state=lambda: self._validate_final_state(
                task_id,
                expected_to_state,
            ),
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
        if effect_type == "create_task":
            payload = effect.get("task")
            if not isinstance(payload, Mapping):
                return _EffectApplyResult(False, "derived task payload invalid", (_error(
                    "task.derived_invalid", "Derived task payload is invalid.",
                ),))
            task = _task_from_mapping(payload)
            created = self.adapter.create_task(task)
            return _EffectApplyResult(created.accepted, "task created", created.errors)
        if effect_type == "link_derived_tasks":
            loaded = self.adapter.read_task(str(effect.get("parent_id", "")))
            if not loaded.accepted or loaded.task is None:
                return _EffectApplyResult(False, "parent task missing", loaded.errors)
            links = tuple(str(link) for link in effect.get("child_links", ()))
            updated = _task_with_derived_links(loaded.task, links)
            written = self.adapter.write_task(updated)
            return _EffectApplyResult(written.accepted, "parent linked", written.errors)
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
        if effect_type == "promote_changed_file":
            source_path = Path(str(effect.get("source_path", "")))
            target_path = Path(str(effect.get("target_path", "")))
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
            except OSError as exc:
                return _EffectApplyResult(False, f"changed file promotion failed: {exc}", (_error(
                    "changed_file.promotion_failed",
                    f"Cannot promote changed file: {exc}",
                    str(target_path),
                ),))
            return _EffectApplyResult(True)
        if effect_type == "commit_repo_changes":
            if self.repo_root is None:
                return _EffectApplyResult(True)
            message = str(effect.get("message", "")).strip()
            paths = tuple(str(path) for path in effect.get("paths", ()))
            committed = _commit_repo_changes(
                repo_root=self.repo_root,
                message=message,
                paths=paths,
                runner=self.repo_command_runner,
            )
            if committed.returncode != 0:
                stderr = (committed.stderr or committed.stdout or "").strip()
                return _EffectApplyResult(False, f"repo commit failed: {stderr}", (_error(
                    "repo.commit_failed",
                    f"Cannot commit accepted changes: {stderr or 'git commit failed'}",
                    str(self.repo_root),
                ),))
            return _EffectApplyResult(True)
        return _EffectApplyResult(False, f"unknown effect: {effect_type}", (_error(
            "effect.unknown",
            f"Unknown completion effect: {effect_type}",
        ),))

    def _compensate_effect(self, effect: Mapping[str, object]) -> _EffectApplyResult:
        effect_type = effect.get("type")
        if effect_type not in {"promote_artifact", "create_task", "link_derived_tasks"}:
            return _EffectApplyResult(True)
        # Derived task creation and parent-linking compensation is intentionally
        # conservative for now; journal recovery can safely replay idempotent
        # accepted records, while adapters reject accidental duplicate IDs.
        if effect_type != "promote_artifact":
            return _EffectApplyResult(True)
        target_path = Path(str(effect.get("target_path", "")))
        target_existed = bool(effect.get("target_existed", False))
        if not target_existed and target_path.exists():
            try:
                target_path.unlink()
            except OSError as exc:
                return _EffectApplyResult(False, f"artifact compensation failed: {exc}", (_error(
                    "artifact.compensation_failed",
                    f"Cannot remove promoted artifact during compensation: {exc}",
                    str(target_path),
                ),))
        previous_links = effect.get("previous_links")
        task_id = str(effect.get("task_id", ""))
        if isinstance(previous_links, (list, tuple)):
            loaded = self.adapter.read_task(task_id)
            if loaded.accepted and loaded.task is not None:
                restored = _task_with_links(loaded.task, tuple(str(link) for link in previous_links))
                written = self.adapter.write_task(restored)
                if not written.accepted:
                    return _EffectApplyResult(False, "artifact link compensation failed", written.errors)
        return _EffectApplyResult(True)

    def _validate_final_state(
        self,
        task_id: str,
        expected_to_state: str,
    ) -> _EffectApplyResult:
        loaded = self.adapter.read_task(task_id)
        if not loaded.accepted or loaded.task is None:
            return _EffectApplyResult(False, "task missing after apply", loaded.errors or (_error(
                "task.not_found",
                f"Task {task_id!r} was not found after mutation.",
                task_id,
            ),))
        if loaded.task.current_state != expected_to_state:
            return _EffectApplyResult(False, "task final state mismatch", (_error(
                "transaction.final_state_invalid",
                (
                    f"Task {task_id!r} ended in {loaded.task.current_state!r}, "
                    f"expected {expected_to_state!r}."
                ),
                task_id,
            ),))
        return _EffectApplyResult(True)


def _format_errors(errors: tuple[DomainError, ...]) -> str:
    return "; ".join(f"{error.code}: {error.message}" for error in errors)


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
    existing_task: Task | None,
) -> tuple[Mapping[str, object], ...]:
    if artifact_root is None:
        return ()
    planned: list[Mapping[str, object]] = []
    for artifact in artifacts:
        source = output_dir / artifact.path
        file_name = Path(artifact.path).name
        if artifact.type == "ImplementationContract":
            try:
                content_hash = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
            except OSError:
                content_hash = "unreadable"
            source_name = Path(file_name)
            file_name = f"{source_name.stem}-{content_hash}{source_name.suffix}"
        target = artifact_root / task_id / artifact.type / file_name
        planned.append({
            "artifact_type": artifact.type,
            "source_path": str(source),
            "target_path": str(target),
            "link": _artifact_link(artifact_root, target),
            "target_existed": target.exists(),
            "previous_links": tuple(existing_task.artifact_links) if existing_task is not None else (),
        })
    return tuple(planned)


def _changed_file_plan(
    *,
    repo_root: Path | None,
    workspace: Path,
    changed_files: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    if repo_root is None:
        return ()
    workspace_root = workspace.resolve()
    repository_root = repo_root.resolve()
    planned: list[Mapping[str, object]] = []
    for ref in changed_files:
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        source = (workspace_root / relative).resolve()
        target = (repository_root / relative).resolve()
        if source != workspace_root and workspace_root not in source.parents:
            continue
        if target != repository_root and repository_root not in target.parents:
            continue
        if source.is_file():
            if target.is_file() and _same_file_content(source, target):
                continue
            planned.append({
                "source_path": str(source),
                "target_path": str(target),
            })
    return tuple(planned)


def _same_file_content(left: Path, right: Path) -> bool:
    try:
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _commit_plan(
    *,
    repo_root: Path | None,
    changed_files: tuple[Mapping[str, object], ...],
    task: Task | None,
) -> Mapping[str, object] | None:
    if repo_root is None or task is None or not changed_files:
        return None
    if not (repo_root / ".git").exists():
        return None
    paths: list[str] = []
    repository_root = repo_root.resolve()
    for item in changed_files:
        target = Path(str(item["target_path"])).resolve()
        try:
            paths.append(str(target.relative_to(repository_root)))
        except ValueError:
            continue
    if not paths:
        return None
    return {
        "type": "commit_repo_changes",
        "message": _commit_message(task),
        "paths": tuple(paths),
    }


def _commit_message(task: Task) -> str:
    title = " ".join(task.title.split()).strip()
    if task.id and title:
        return f"{task.id}: {title}"
    return title or f"Task {task.id}"


def _commit_repo_changes(
    *,
    repo_root: Path,
    message: str,
    paths: tuple[str, ...],
    runner,
) -> subprocess.CompletedProcess[str]:
    command_runner = runner or _run_repo_command
    committable_paths = _git_committable_paths(
        repo_root=repo_root,
        paths=paths,
        runner=command_runner,
    )
    if not committable_paths:
        return subprocess.CompletedProcess(("git", "add", "--", *paths), 0, "", "")
    added = command_runner(("git", "add", "--", *committable_paths), repo_root)
    if added.returncode != 0:
        return added
    committed = command_runner(("git", "commit", "-m", message, "--", *committable_paths), repo_root)
    if committed.returncode != 0 and _git_nothing_to_commit(committed):
        return subprocess.CompletedProcess(committed.args, 0, committed.stdout, committed.stderr)
    return committed


def _git_committable_paths(
    *,
    repo_root: Path,
    paths: tuple[str, ...],
    runner,
) -> tuple[str, ...]:
    committable: list[str] = []
    for path in paths:
        ignored = runner(("git", "check-ignore", "-q", "--", path), repo_root)
        if ignored.returncode == 0:
            continue
        committable.append(path)
    return tuple(committable)


def _git_nothing_to_commit(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return "nothing to commit" in output and "working tree clean" in output


def _run_repo_command(command: tuple[str, ...], repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )


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


def _task_with_derived_links(task: Task, child_links: tuple[str, ...]) -> Task:
    body = task.body.rstrip()
    section = "\n\n## Derived tasks\n" + "\n".join(f"- [[{link}]]" for link in child_links) + "\n"
    return Task(
        id=task.id,
        title=task.title,
        path=task.path,
        current_state=task.current_state,
        task_type=task.task_type,
        dependencies=task.dependencies,
        artifact_links=task.artifact_links,
        parent_id=task.parent_id,
        metadata=task.metadata,
        body=body + section,
    )


def _task_to_dict(task: Task) -> Mapping[str, object]:
    return {
        "id": task.id,
        "title": task.title,
        "path": task.path,
        "current_state": task.current_state,
        "task_type": task.task_type,
        "dependencies": tuple(task.dependencies),
        "artifact_links": tuple(task.artifact_links),
        "parent_id": task.parent_id,
        "metadata": dict(task.metadata),
        "body": task.body,
    }


def _task_from_mapping(payload: Mapping[str, object]) -> Task:
    return Task(
        id=str(payload["id"]),
        title=str(payload["title"]),
        path=str(payload["path"]),
        current_state=str(payload["current_state"]),
        task_type=str(payload.get("task_type", "task")),
        dependencies=tuple(str(item) for item in payload.get("dependencies", ())),
        artifact_links=tuple(str(item) for item in payload.get("artifact_links", ())),
        parent_id=str(payload["parent_id"]) if payload.get("parent_id") is not None else None,
        metadata=dict(payload.get("metadata", {})),
        body=str(payload.get("body", "")),
    )


def _derived_task_plan(
    *,
    output_dir: Path,
    transition,
    artifacts: tuple[ArtifactSubmission, ...],
    parent_id: str,
    existing_task_ids: tuple[str, ...] = (),
) -> tuple[tuple[Mapping[str, object], ...], tuple[DomainError, ...]]:
    if transition.derives is None:
        return (), ()
    selected = tuple(artifact for artifact in artifacts if artifact.type == transition.derives.artifact_type)
    parsed: list[tuple[str, str, tuple[str, ...], str]] = []
    errors: list[DomainError] = []
    local_ids: set[str] = set()
    for artifact in selected:
        path = output_dir / artifact.path
        try:
            local_id, title, dependencies, body = _parse_derived_task_file(path)
        except ValueError as exc:
            errors.append(_error("task.derived_invalid", str(exc), artifact.path))
            continue
        if local_id in local_ids:
            errors.append(_error("task.derived_duplicate_local_id", f"Duplicate derived task local_id: {local_id}", local_id))
        local_ids.add(local_id)
        parsed.append((local_id, title, dependencies, body))
    if errors:
        return (), tuple(errors)
    ids = _allocate_numeric_task_ids(tuple(local_id for local_id, *_ in parsed), existing_task_ids)
    planned: list[Mapping[str, object]] = []
    for local_id, title, dependencies, body in parsed:
        unknown = tuple(dep for dep in dependencies if dep not in ids)
        if unknown:
            errors.append(_error(
                "task.derived_unknown_dependency",
                f"Derived task {local_id!r} references unknown local dependencies: {', '.join(unknown)}",
                local_id,
            ))
            continue
        task_id = ids[local_id]
        task = Task(
            id=task_id,
            title=title,
            path=f"tasks/{task_id}.md",
            current_state=transition.derives.state,
            task_type=transition.derives.task_type,
            dependencies=tuple(ids[dep] for dep in dependencies),
            parent_id=parent_id,
            body=body,
        )
        planned.append({"task": task, "link": f"{task_id}-{_slugify(title)}"})
    return tuple(planned), tuple(errors)


NUMERIC_TASK_ID_RE = re.compile(r"^[1-9][0-9]*$")


def _allocate_numeric_task_ids(local_ids: tuple[str, ...], existing_task_ids: tuple[str, ...]) -> dict[str, str]:
    highest = 0
    for task_id in existing_task_ids:
        if NUMERIC_TASK_ID_RE.match(task_id):
            highest = max(highest, int(task_id))
    return {
        local_id: str(next_id)
        for local_id, next_id in zip(local_ids, range(highest + 1, highest + 1 + len(local_ids)))
    }


def _parse_derived_task_file(path: Path) -> tuple[str, str, tuple[str, ...], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("Derived task artifact must start with YAML frontmatter.")
    try:
        _, raw_frontmatter, body = text.split("---", 2)
        parsed = YAML(typ="safe").load(StringIO(raw_frontmatter)) or {}
    except Exception as exc:
        raise ValueError(f"Derived task frontmatter is invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Derived task frontmatter must be a mapping.")
    local_id = str(parsed.get("local_id", "")).strip()
    if not local_id:
        raise ValueError("Derived task frontmatter requires local_id.")
    dependencies_raw = parsed.get("dependencies")
    if dependencies_raw is None:
        dependencies = ()
    elif isinstance(dependencies_raw, str):
        dependencies = (dependencies_raw,)
    elif isinstance(dependencies_raw, list):
        dependencies = tuple(str(item) for item in dependencies_raw)
    else:
        raise ValueError("Derived task dependencies must be a string or list.")
    title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
    if not title:
        raise ValueError("Derived task body requires a Markdown H1 title.")
    return local_id, title, dependencies, body


def _slugify(value: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower() or "task"


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


def recover_completion_transactions(
    *,
    service: CompletionService,
    event_store: JsonlEventStore,
    journal_store: TransactionJournalStore,
) -> tuple[str, ...]:
    recovered: list[str] = []
    existing_event_ids = {event.event_id for event in event_store.iter_events()}
    for record in journal_store.list_incomplete():
        effect_types = {effect.get("type") for effect in record.effects}
        if not effect_types or not effect_types.issubset({"promote_artifact", "move_task", "create_task", "link_derived_tasks"}):
            continue
        if record.task_id is None or record.transition_id is None:
            continue
        all_effects_ok = True
        for effect in record.effects:
            result = service._apply_effect(effect)
            if not result.accepted:
                all_effects_ok = False
                break
        if not all_effects_ok:
            continue
        expected_to_state = next(
            (
                str(effect.get("to_state", ""))
                for effect in record.effects
                if effect.get("type") == "move_task"
                and str(effect.get("task_id", "")) == record.task_id
            ),
            "",
        )
        if not expected_to_state:
            continue
        final = service._validate_final_state(record.task_id, expected_to_state)
        if not final.accepted:
            continue
        missing_events = tuple(event for event in record.events if event.event_id not in existing_event_ids)
        if missing_events:
            appended = event_store.append_many(missing_events)
            if not appended.accepted:
                continue
            existing_event_ids.update(event.event_id for event in missing_events)
        committed = journal_store.commit(record)
        if committed.accepted:
            recovered.append(record.journal_id)
    return tuple(recovered)
