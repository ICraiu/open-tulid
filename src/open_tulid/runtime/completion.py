from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from io import StringIO

from ruamel.yaml import YAML

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import DomainError, EventActor, EventType, ExecutionJobStatus, Task, WorkflowDefinition
from open_tulid.runtime.events import JsonlEventStore, build_event, new_ulid, utc_now
from open_tulid.runtime.execution_contracts import load_job_execution_contract
from .planning_inputs import load_planning_inputs
from open_tulid.runtime.pathops import (
    SourcePathError,
    copy_regular_nofollow,
    rmdir_if_empty_nofollow,
    unlink_regular_nofollow,
)
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.transactions import FileTransactionRuntime
from open_tulid.runtime.events import TransactionJournalStore
from open_tulid.vault.task_schema import validate_task_structure, validate_task_schema
from .task_contracts import task_uses_global_contract

from .verifier import (
    ArtifactSubmission,
    CompletionSubmission,
    DeterministicVerifier,
    VerificationResult,
    _validate_review_result,
    normalize_artifacts,
)
from .repairs import DEFAULT_MAX_REPAIR_ATTEMPTS, plan_repair
from .candidate import (
    KIND_DELETE,
    Candidate,
    CandidateChange,
    capture_candidate,
    capture_deliverable_manifest,
    source_selection_from_dict,
)
from .verification_runtime import (
    ContainerCommandExecutor, VerificationEnvironment, environment_identity_of,
    prepare_verification_copy,
)
from .repository_facts import capture_repository_snapshot, repository_identity
from .prompts import is_review_transition


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


@contextlib.contextmanager
def _publication_lock(root: object):
    """Serialize derived-task batch publication within one shared event store.

    Two concurrent ``submit`` calls that derive children both read the same
    tracker snapshot to choose the next numeric task IDs. Reading and applying
    must be atomic, otherwise two batches can allocate overlapping IDs or leak a
    cross-parent link. This lock is keyed on the shared event-store root so
    sibling batches in the same project serialize at the allocation boundary,
    while unrelated batches in other projects remain independent.
    """
    from pathlib import Path as _Path

    lock_path = _Path(str(root)) / "derived-batch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        candidate_root: Path | None = None,
        verifier: DeterministicVerifier | None = None,
        validation_implementations: Mapping[str, object] | None = None,
        validation_context_factory: object | None = None,
        verification_executor: object | None = None,
        verification_environment_identity: str | None = None,
        max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.event_store = event_store
        self.journal_store = journal_store or TransactionJournalStore(event_store.root / "journals")
        self.artifact_root = artifact_root
        self.repo_root = repo_root
        self.repo_command_runner = repo_command_runner
        self.candidate_root = candidate_root
        passed_verifier = verifier or DeterministicVerifier(
            artifact_templates={
                artifact_id: artifact.template
                for artifact_id, artifact in workflow.artifact_types.items()
            },
            validation_implementations=validation_implementations,
            validation_context_factory=validation_context_factory,
            executor=verification_executor,
            environment_identity=verification_environment_identity,
        )
        self.verifier = passed_verifier
        self.verification_executor = verification_executor
        self.verification_environment_identity = verification_environment_identity
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
        try:
            planning = load_planning_inputs(job)
        except (ValueError, TypeError, KeyError) as exc:
            return CompletionResult(False, errors=(_error("prompt.frozen_invalid", str(exc), job.job_id),))
        transition = (
            frozen.contract.transition
            if frozen.contract is not None
            else planning.transition if planning is not None
            else self.workflow.transitions.get(job.transition_id)
        )
        if transition is None:
            return CompletionResult(False, errors=(_error(
                "transition.not_found",
                f"Transition {job.transition_id!r} is not defined.",
                job.transition_id,
            ),))
        if is_review_transition(transition):
            review_result_errors = _validate_review_result(submission.review_result)
            if review_result_errors:
                return CompletionResult(False, errors=review_result_errors)

        actor = EventActor(type="executor", id=job.worker_id)
        submission_data = {
            "summary": submission.summary,
            "attempt": submission.attempt,
            "artifacts": [_artifact_to_dict(artifact) for artifact in submission.artifacts],
            "changed_files": list(submission.changed_files),
            "validation_evidence": dict(submission.validation_evidence),
        }
        if submission.review_result is not None:
            submission_data["review_result"] = dict(submission.review_result)
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=actor,
            event_type=EventType.ExecutionCompletionSubmitted,
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            submission_id=submission_id,
            data=submission_data,
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
        captured = self._capture_completion_candidate(
            job=job,
            baseline=frozen.contract.baseline_manifest if frozen.contract is not None else None,
            submission_id=submission_id,
            submission=submission,
            selection=(
                frozen.contract.repository_facts.source_selection
                if frozen.contract is not None
                else None
            ),
        )
        if not captured.accepted:
            return self._reject_candidate_capture(
                job=job,
                submission_id=submission_id,
                errors=captured.errors,
            )
        candidate = captured.captured.candidate
        self.event_store.append(build_event(
            project_id=job.project_id,
            actor=EventActor(type="system", id="completion-verifier"),
            event_type="ExecutionCompletionCandidateCaptured",
            correlation_id=job.job_id,
            task_id=job.task_id,
            job_id=job.job_id,
            transition_id=job.transition_id,
            submission_id=submission_id,
            data={
                "candidate_id": candidate.candidate_id,
                "candidate_sha256": candidate.sha256,
                "manifest_sha256": candidate.manifest_sha256,
                "change_count": len(candidate.changes),
                "changed_files": [change.path for change in candidate.changes],
                "submitted_discrepancy": candidate.submitted_discrepancy,
            },
        ))
        try:
            output_relative = _relative_output_path(
                job, Path(str(job.metadata.get("output_path", Path(job.workspace_path) / "output"))),
            )
            if output_relative is None:
                raise ValueError("Artifact output must be a directory within the captured workspace.")
            verification_workspace = prepare_verification_copy(
                candidate_path=captured.captured.storage_path,
                writable_root=captured.captured.storage_path.parent / "verification",
                copy_id=candidate.candidate_id,
            )
            executor = self.verification_executor
            environment_identity = self.verification_environment_identity
            if frozen.contract is not None:
                if executor is None:
                    # Durable, sanitized environment frozen before the worker
                    # starts. CLI submission/recovery uses the same identity.
                    environment = VerificationEnvironment(
                        **dict(job.metadata.get("verification_environment") or {})
                    )
                    executor = ContainerCommandExecutor(environment=environment)
                    environment_identity = environment_identity_of(environment)
            verification = self.verifier.verify(
                workspace=verification_workspace,
                output_dir=verification_workspace / output_relative,
                transition=transition,
                submission=submission,
                execution_contract=frozen.contract,
                candidate_id=candidate.candidate_id,
                candidate_manifest_sha256=candidate.manifest_sha256,
                executor=executor,
                environment_identity=environment_identity,
                review_transition=is_review_transition(transition),
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

        output_dir = captured.captured.storage_path / output_relative
        submitted_artifacts = normalize_artifacts(submission.artifacts)
        artifact_paths = tuple((Path(output_relative) / artifact.path).as_posix()
                               for artifact in submitted_artifacts)
        baseline_paths = ({entry.path for entry in frozen.contract.baseline_manifest.entries}
                          if frozen.contract is not None else set())
        shadowed = tuple(path for path in artifact_paths
                         if path in baseline_paths or
                         (self.repo_root is not None and (self.repo_root / path).exists()))
        if shadowed:
            errors = (_error(
                "completion.artifact_source_conflict",
                "Submitted artifacts overlap repository source; use separate artifact paths: "
                + ", ".join(shadowed),
            ),)
            return self._reject_completion(
                job=job, submission_id=submission_id, verification=verification,
                errors=errors, message=_format_errors(errors),
            )
        promoted_artifacts = _promotion_plan(
            artifact_root=self.artifact_root,
            output_dir=output_dir,
            task_id=job.task_id,
            artifacts=submitted_artifacts,
            existing_task=self.adapter.read_task(job.task_id).task,
        )
        promoted_files = _candidate_change_plan(
            repo_root=self.repo_root,
            candidate_storage=captured.captured.storage_path,
            changes=candidate.changes,
            output_relative=output_relative,
            artifact_paths=artifact_paths,
        )
        commit_effect = _commit_plan(
            repo_root=self.repo_root,
            changed_files=promoted_files,
            task=self.adapter.read_task(job.task_id).task,
        )
        target_check = _check_integration_target(
            repo_root=self.repo_root,
            repo_identity=repository_identity(self.repo_root),
            contract=frozen.contract,
        )
        if not target_check.accepted:
            return self._reject_completion(
                job=job,
                submission_id=submission_id,
                verification=verification,
                errors=target_check.errors,
                message=_format_errors(target_check.errors),
            )
        with _publication_lock(self.event_store.root):
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
                artifacts=submitted_artifacts,
                parent_id=job.task_id,
                existing_task_ids=existing_task_ids,
                workflow=self.workflow,
                promoted_artifact_links={
                    (artifact.type, artifact.path): str(plan["link"])
                    for artifact, plan in zip(submitted_artifacts, promoted_artifacts)
                },
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
                *(dict(item) for item in promoted_files),
                *((commit_effect,) if commit_effect is not None else ()),
                *(
                    {
                        **dict(item),
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
                expected_previous_state=transition.from_state,
                effects=effects,
                events=events,
                journal_id=f"{job.job_id}-{submission_id}",
                candidate=candidate,
                commit_effect=commit_effect,
                artifact_destinations=tuple(str(item["target_path"]) for item in promoted_artifacts),
                output_relative=output_relative,
                artifact_paths=artifact_paths,
                acceptance_context={
                    "job_id": job.job_id,
                    "task_revision": _acceptance_task_revision(job, frozen.contract, self.adapter),
                    "verification_accepted": verification.accepted,
                    "verification_report": verification.report.to_dict() if verification.report else None,
                    "submission_id": submission_id,
                    "output_relative": output_relative,
                    "artifact_paths": artifact_paths,
                    "acceptance_metadata": {
                        "completed_submission_id": submission_id,
                        "promoted_artifacts": tuple(promoted_artifacts),
                        "promoted_files": tuple(promoted_files),
                        "acceptance_transaction_id": f"{job.job_id}-{submission_id}",
                        "acceptance_repository_identity": repository_identity(self.repo_root),
                        **({"review_result": dict(submission.review_result)} if submission.review_result is not None else {}),
                        "completion_submissions": _record_submission(
                            job.metadata, submission_id, accepted=True, feedback=(),
                        ),
                    },
                },
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
                "acceptance_transaction_id": f"{job.job_id}-{submission_id}",
                "acceptance_repository_identity": repository_identity(self.repo_root),
                **(
                    {"acceptance_repository_commit": _accepted_commit_sha(
                        repo_root=self.repo_root,
                        runner=self.repo_command_runner,
                    )}
                    if self.repo_root is not None else {}
                ),
                **({"review_result": dict(submission.review_result)} if submission.review_result is not None else {}),
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
            "retry_reason": repair.reason,
        })
        terminal_rejection = not repair.eligible
        self.job_store.update_status(
            job.job_id,
            (ExecutionJobStatus.COMPLETION_REJECTED if not terminal_rejection else ExecutionJobStatus.FAILED),
            metadata={
                "last_verification": message,
                "repair_ready": repair.eligible,
                "repair_packet": repair.packet,
                "repair_blocked_reason": repair.reason,
                "retry_reason": repair.reason,
                "retry_attempt": int(metadata.get("repair_attempts", 0)),
                "repair_history": tuple(repair_history),
                **({"failure_reason": f"completion_rejected:{repair.reason}"} if terminal_rejection else {}),
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

    def _capture_completion_candidate(
        self,
        *,
        job,
        baseline,
        submission_id: str,
        submission: CompletionSubmission,
        selection=None,
    ):
        storage_root = self.candidate_root
        if storage_root is None:
            storage_root = Path(job.workspace_path).parent / ".candidates"
        # Submission identifiers are worker supplied and only unique within a
        # job. Never use them directly as a shared filesystem path.
        candidate_id = hashlib.sha256(
            json.dumps([job.job_id, submission_id]).encode("utf-8")
        ).hexdigest()
        result = capture_candidate(
            workspace=Path(job.workspace_path),
            storage_root=storage_root,
            candidate_id=candidate_id,
            baseline=baseline,
            submitted_changed_files=submission.changed_files,
            selection=selection,
        )
        if not result.accepted or result.captured is None:
            return result
        candidate = result.captured.candidate
        current = self.job_store.get(job.job_id)
        current_status = current.job.status if current.accepted and current.job is not None else job.status
        self.job_store.update_status(
            job.job_id,
            current_status,
            metadata={
                "active_candidate_id": candidate.candidate_id,
                "active_candidate_sha256": candidate.sha256,
                "active_candidate_manifest_sha256": candidate.manifest_sha256,
                "active_candidate_storage_path": candidate.storage_path,
                "active_candidate_changes": tuple(
                    {
                        "kind": change.kind,
                        "path": change.path,
                        "before_sha256": change.before_sha256,
                        "after_sha256": change.after_sha256,
                        "before_mode": change.before_mode,
                        "after_mode": change.after_mode,
                    }
                    for change in candidate.changes
                ),
                "active_candidate_submitted_discrepancy": candidate.submitted_discrepancy,
            },
        )
        return result

    def _reject_candidate_capture(
        self,
        *,
        job,
        submission_id: str,
        errors: tuple[DomainError, ...],
    ) -> CompletionResult:
        current = self.job_store.get(job.job_id)
        metadata = current.job.metadata if current.accepted and current.job is not None else job.metadata
        repair_history = list(metadata.get("repair_history", ()))
        repair_history.append({
            "submission_id": submission_id,
            "classification": "candidate_capture",
            "error_codes": [error.code for error in errors],
            "repair_ready": True,
            "retry_reason": "candidate_capture_failed",
        })
        self.job_store.update_status(
            job.job_id,
            ExecutionJobStatus.COMPLETION_REJECTED,
            metadata={
                "last_verification": "Candidate could not be captured stably.",
                "repair_ready": True,
                "repair_blocked_reason": None,
                "retry_reason": "candidate_capture_failed",
                "repair_history": tuple(repair_history),
                "candidate_capture_errors": tuple(error.code for error in errors),
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
        return CompletionResult(False, errors=errors)

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
        expected_previous_state: str | None,
        effects: tuple[Mapping[str, object], ...],
        events: tuple[object, ...],
        journal_id: str,
        candidate: Candidate | None = None,
        commit_effect: Mapping[str, object] | None = None,
        artifact_destinations: tuple[str, ...] = (),
        output_relative: str | None = None,
        artifact_paths: tuple[str, ...] | None = None,
        acceptance_context: Mapping[str, object] | None = None,
    ) -> _EffectApplyResult:
        context: dict[str, object] = dict(acceptance_context or {})
        if candidate is not None:
            context.update({
                "candidate": candidate.to_dict(),
                "candidate_id": candidate.candidate_id,
                "candidate_sha256": candidate.sha256,
                "candidate_manifest_sha256": candidate.manifest_sha256,
                "candidate_storage_path": candidate.storage_path,
            })
        context.update({
            "expected_previous_state": str(expected_previous_state) if expected_previous_state is not None else None,
            "expected_to_state": expected_to_state,
            "artifact_destinations": tuple(artifact_destinations),
        })
        repo_identity = repository_identity(self.repo_root)
        if repo_identity is not None:
            context["repository_identity"] = repo_identity
        if self.repo_root is not None:
            snapshot = capture_repository_snapshot(self.repo_root)
            base_commit = None
            if snapshot.accepted and snapshot.snapshot is not None:
                base_commit = snapshot.snapshot.facts.base_commit
            context["repository_base_commit"] = base_commit
        if commit_effect is not None:
            # R4: persist the intended repository/tree identity (and the base it
            # derives from) before the commit effect so recovery can verify the
            # actual committed content, not merely a transaction-shaped tip with
            # a matching subject. When the intended tree cannot be proven the
            # record simply lacks tree proof and recovery treats it unresolved.
            intended_tree = (
                _intended_commit_tree(
                    repo_root=self.repo_root,
                    base=base_commit,
                    effects=effects,
                )
                if base_commit is not None
                else None
            )
            context["commit"] = {
                "message": str(commit_effect.get("message", "")),
                "paths": tuple(str(path) for path in commit_effect.get("paths", ())),
                "expected_outcome": "committed",
                "repository_base_commit": str(base_commit) if base_commit is not None else None,
                "intended_tree": intended_tree,
            }

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
                repo_root=self.repo_root,
                candidate=candidate,
                output_relative=output_relative,
                artifact_paths=artifact_paths,
            ),
        )
        applied = runtime.apply(
            project_id=project_id,
            task_id=task_id,
            transition_id=transition_id,
            effects=effects,
            events=events,
            journal_id=journal_id,
            context=context,
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
        if effect_type == "delete_changed_file":
            target_path = Path(str(effect.get("target_path", "")))
            delete_result = _delete_changed_nofollow(
                target_path=target_path,
                repo_root=self.repo_root,
            )
            if not delete_result.accepted:
                return _EffectApplyResult(
                    False,
                    delete_result.error.message if delete_result.error is not None else "changed file delete conflict",
                    tuple(delete_result.errors),
                )
            return _EffectApplyResult(True)
        if effect_type == "promote_changed_file":
            source_path = Path(str(effect.get("source_path", "")))
            target_path = Path(str(effect.get("target_path", "")))
            promote_result = _promote_changed_nofollow(
                source_path=source_path,
                target_path=target_path,
                repo_root=self.repo_root,
                after_mode=effect.get("expected_after_mode"),
            )
            if not promote_result.accepted:
                return _EffectApplyResult(
                    False,
                    promote_result.error.message if promote_result.error is not None else "changed file promotion failed",
                    tuple(promote_result.errors),
                )
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
            expected_after = effect.get("expected_after_sha256")
            if (
                isinstance(expected_after, str)
                and _file_sha256(target_path) != expected_after
            ):
                return _EffectApplyResult(False, "artifact compensation blocked by intervening change", (_error(
                    "artifact.compensation_conflict",
                    f"Cannot compensate promoted artifact {target_path}: content changed since promotion.",
                    str(target_path),
                ),))
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
        *,
        repo_root: Path | None = None,
        candidate: Candidate | None = None,
        output_relative: str | None = None,
        artifact_paths: tuple[str, ...] | None = None,
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
        if repo_root is not None and candidate is not None:
            integrated_errors = _validate_integrated_source(
                repo_root=repo_root,
                candidate=candidate,
                output_relative=output_relative,
                artifact_paths=artifact_paths,
            )
            if integrated_errors:
                return _EffectApplyResult(
                    False,
                    "integrated deliverable manifest does not match the verified candidate",
                    tuple(integrated_errors),
                )
        return _EffectApplyResult(True)


def _format_errors(errors: tuple[DomainError, ...]) -> str:
    return "; ".join(f"{error.code}: {error.message}" for error in errors)


def _acceptance_task_revision(job, contract, adapter):
    from .attempts import task_semantic_revision
    from .execution_contracts import source_content_identities
    planning = load_planning_inputs(job)
    if contract is None and planning is not None:
        return task_semantic_revision(planning.source_task, source_identities=planning.source_identities)
    task = contract.source_task if contract is not None else adapter.read_task(job.task_id).task
    return task_semantic_revision(task, source_identities=source_content_identities(contract) if contract else ())


@dataclass(frozen=True)
class _IntegrationTargetCheck:
    accepted: bool
    errors: tuple[DomainError, ...] = ()


def _check_integration_target(
    *,
    repo_root: Path | None,
    repo_identity: str | None,
    contract,
) -> _IntegrationTargetCheck:
    """Check the integration target before mutating it.

    Compares the target's baseline/branch identity and deliverable manifest with
    the ones captured for the job. If the source changed since the baseline, the
    candidate is preserved and integration is halted with a stale/conflict
    report rather than copying verified bytes over a newer repository or
    absorbing live user changes into an automated commit. A Git-backed automatic
    commit additionally requires a clean managed checkout at both baseline and
    integration time.
    """
    if repo_root is None or contract is None:
        return _IntegrationTargetCheck(accepted=True)
    current = capture_repository_snapshot(repo_root)
    if not current.accepted or current.snapshot is None:
        return _IntegrationTargetCheck(
            accepted=False,
            errors=current.errors or (_error(
                "repo.target_unreadable",
                "Cannot scan the integration target before mutation.",
                str(repo_root),
            ),),
        )
    now = current.snapshot
    baseline_manifest = contract.baseline_manifest
    baseline_facts = contract.repository_facts
    drift: list[str] = []
    if now.baseline.sha256 != baseline_manifest.sha256:
        drift.append("deliverable manifest changed since the job baseline")
    if now.facts.git_repository and baseline_facts.git_repository:
        if (
            now.facts.base_commit is not None
            and baseline_facts.base_commit is not None
            and now.facts.base_commit != baseline_facts.base_commit
        ):
            drift.append("branch base commit moved since the job baseline")
        if baseline_facts.dirty:
            drift.append("target was not a clean managed checkout at baseline")
        if now.facts.dirty:
            drift.append("target has live uncommitted changes")
    if not drift:
        return _IntegrationTargetCheck(accepted=True)
    return _IntegrationTargetCheck(
        accepted=False,
        errors=(_error(
            "repo.stale_target",
            (
                "Integration target no longer matches the job baseline "
                f"(repository identified as {repo_identity}); candidate preserved: "
                + "; ".join(drift)
                + ". Stop rather than overwriting the repository with the stale "
                "candidate; re-plan against the current repository."
            ),
            str(repo_root.resolve()),
        ),),
    )


def _relative_output_path(job, output_dir: Path) -> str | None:
    """Artifact output path as a source-root-relative path.

    The artifact output subtree is transported separately by ``promote_artifact``,
    so source promotion and integrated validation must exclude it consistently
    whichever root (sealed candidate or repository) they resolve against.
    """
    workspace = Path(job.workspace_path).resolve()
    try:
        resolved = output_dir.resolve()
        relative = resolved.relative_to(workspace).as_posix()
    except ValueError:
        return None
    return relative if relative != "." else None


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
        try:
            expected_after_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError:
            expected_after_sha256 = None
        planned.append({
            "artifact_type": artifact.type,
            "source_path": str(source),
            "target_path": str(target),
            "link": _artifact_link(artifact_root, target),
            "target_existed": target.exists(),
            "previous_links": tuple(existing_task.artifact_links) if existing_task is not None else (),
            "expected_after_sha256": expected_after_sha256,
            "expected_before_sha256": _file_sha256(target) if target.is_file() else None,
            "expected_before_mode": target.stat().st_mode & 0o777 if target.is_file() else None,
            "expected_after_mode": source.stat().st_mode & 0o777,
        })
    return tuple(planned)


def _candidate_change_plan(
    *,
    repo_root: Path | None,
    candidate_storage: Path,
    changes: tuple[CandidateChange, ...],
    output_relative: str | None = None,
    artifact_paths: tuple[str, ...] | None = None,
) -> tuple[Mapping[str, object], ...]:
    # Source promotion is driven by the sealed candidate's authoritative change
    # set (plan 5D), never the worker's submitted list, so an omitted or stale
    # list cannot cause silent partial transport. Only explicitly submitted
    # artifact paths are transported separately by promote_artifact. Historical
    # journals without an exact list retain their original subtree rule.
    if repo_root is None:
        return ()
    workspace_root = candidate_storage.resolve()
    repository_root = repo_root.resolve()
    artifact_source = (
        (workspace_root / output_relative).resolve()
        if output_relative is not None else None
    )
    artifact_target = (
        (repository_root / output_relative).resolve()
        if output_relative is not None else None
    )
    planned: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for change in changes:
        if artifact_paths is not None and change.path in artifact_paths:
            continue
        relative = Path(change.path)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if any(part == ".open-tulid" for part in relative.parts):
            continue
        if change.path in seen:
            continue
        source = (workspace_root / relative).resolve()
        target = (repository_root / relative).resolve()
        if source != workspace_root and workspace_root not in source.parents:
            continue
        if target != repository_root and repository_root not in target.parents:
            continue
        if (
            artifact_paths is None and artifact_source is not None
            and (source == artifact_source or artifact_source in source.parents)
        ):
            continue
        if (
            artifact_paths is None and artifact_target is not None
            and (target == artifact_target or artifact_target in target.parents)
        ):
            continue
        seen.add(change.path)
        if change.kind == KIND_DELETE:
            planned.append({
                "type": "delete_changed_file",
                "target_path": str(target),
                "expected_after_listing": "absent",
                "expected_before_sha256": change.before_sha256,
            })
        elif source.is_file():
            if target.is_file() and _same_file_content(source, target):
                continue
            planned.append({
                "type": "promote_changed_file",
                "source_path": str(source),
                "target_path": str(target),
                "expected_after_sha256": change.after_sha256,
                "expected_before_sha256": change.before_sha256,
                "expected_before_mode": change.before_mode,
                "expected_after_mode": change.after_mode,
                "target_existed": target.exists(),
            })
    # R3: a file/directory transition (or any delete that makes a path writable
    # again) must run before the promote that replaces it. Emitting every delete
    # before every promote guarantees the deletes that free a parent path run
    # first, and an empty leftover directory can be removed/created without
    # racing a promotion of the same-or-ancestor path.
    deletes = sorted(
        (item for item in planned if item.get("type") == "delete_changed_file"),
        key=lambda item: str(item.get("target_path", "")),
    )
    promotes = sorted(
        (item for item in planned if item.get("type") != "delete_changed_file"),
        key=lambda item: str(item.get("target_path", "")),
    )
    return tuple(deletes + promotes)


def _validate_integrated_source(
    *,
    repo_root: Path,
    candidate: Candidate,
    output_relative: str | None = None,
    artifact_paths: tuple[str, ...] | None = None,
) -> tuple[DomainError, ...]:
    repository_root = repo_root.resolve()
    artifact_target = (
        (repository_root / output_relative).resolve()
        if output_relative is not None else None
    )
    errors: list[DomainError] = []
    # Validate the complete source, including unchanged files. Verifying only
    # the delta could bless an unrelated edit made during acceptance/recovery.
    try:
        def surface(root, selection=None):
            return {
                entry.path: (entry.sha256, entry.mode)
                for entry in capture_deliverable_manifest(root, selection).entries
                if (entry.path not in artifact_paths if artifact_paths is not None else
                    output_relative is None or not (
                        entry.path == output_relative or entry.path.startswith(output_relative + "/")
                    ))
            }
        # The sealed candidate applies its own frozen selection (tracked source
        # under cache names is retained); the live target re-discovers the same
        # Git/non-Git rule. Both selection surfaces must agree for transport.
        candidate_selection = getattr(candidate, "source_selection", None)
        if surface(Path(candidate.storage_path), candidate_selection) != surface(repository_root):
            errors.append(_error("transaction.integrated_manifest_mismatch",
                "Integrated source differs from the complete verified candidate.", str(repository_root)))
    except OSError as exc:
        errors.append(_error("transaction.integrated_manifest_unreadable", str(exc), str(repository_root)))
    for change in candidate.changes:
        if artifact_paths is not None and change.path in artifact_paths:
            continue
        relative = Path(change.path)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if any(part == ".open-tulid" for part in relative.parts):
            continue
        target = (repository_root / relative).resolve()
        if (
            artifact_paths is None and artifact_target is not None
            and (target == artifact_target or artifact_target in target.parents)
        ):
            continue
        if change.kind == KIND_DELETE:
            # The deleted FILE must be gone. In a file -> directory transition the
            # path legitimately becomes a directory hosting the candidate's added
            # children; the full-surface manifest check above already guarantees
            # that directory holds exactly the intended children. A lingering
            # regular file (or a checked-out symlink) is a missed delete.
            if target.exists() and (target.is_file() or target.is_symlink()):
                errors.append(_error(
                    "transaction.integrated_delete_missed",
                    f"Deleted change {change.path!r} still exists in the integrated repository.",
                    str(target),
                ))
        elif not target.is_file():
            errors.append(_error(
                "transaction.integrated_source_missing",
                f"Change {change.path!r} did not reach the integrated repository.",
                str(target),
            ))
        elif ((change.after_sha256 and _file_sha256(target) != change.after_sha256)
              or (change.after_mode is not None and target.stat().st_mode & 0o777 != change.after_mode)):
            errors.append(_error(
                "transaction.integrated_source_mismatch",
                f"Change {change.path!r} content differs from the verified candidate.",
                str(target),
            ))
    return tuple(errors)


def _accepted_commit_sha(*, repo_root: Path | None, runner) -> str | None:
    """The integration commit identity recorded on an accepted completion.

    Includes no-change reviews: their existing verified HEAD is still the
    integrated identity, even when no new commit was needed.
    """
    if repo_root is None or not (repo_root / ".git").exists():
        return None
    command_runner = runner or _run_repo_command
    head = _git_rev(command_runner, repo_root, "HEAD")
    if not head:
        return None
    return head


def _same_file_content(left: Path, right: Path) -> bool:
    try:
        return (left.read_bytes() == right.read_bytes()
                and left.stat().st_mode & 0o777 == right.stat().st_mode & 0o777)
    except OSError:
        return False


@dataclass(frozen=True)
class _ChangedPathApplyResult:
    accepted: bool
    error: DomainError | None = None
    errors: tuple[DomainError, ...] = ()

    @classmethod
    def ok(cls) -> "_ChangedPathApplyResult":
        return cls(accepted=True)

    @classmethod
    def fail(cls, code: str, message: str, location: str | None = None) -> "_ChangedPathApplyResult":
        error = _error(code, message, location)
        return cls(accepted=False, error=error, errors=(error,))


def _strip_relative(abs_path: Path, relative: str) -> Path:
    """Derive the root a relative path was resolved against.

    ``abs_path`` was built as ``root / relative``; strip the trailing relative
    parts to recover ``root`` without re-resolving (used only to name an admitted
    root for the ``dir_fd``/``O_NOFOLLOW`` walk).
    """
    result = abs_path
    for _ in Path(relative).parts:
        result = result.parent
    return result


def _target_relative(target_path: Path, repo_root: Path | None) -> str | None:
    """Lexical target path relative to the admitted root (never re-resolved).

    ``Path.relative_to`` compares path components without touching the
    filesystem, so a target path that is lexically inside ``repo_root`` stays
    inside even if the root is swapped to a link: containment is then enforced by
    the ``O_NOFOLLOW`` directory-fd opens in ``pathops``.
    """
    if repo_root is None:
        return None
    try:
        return str(target_path.relative_to(Path(repo_root)))
    except ValueError:
        return None


def _delete_changed_nofollow(
    *,
    target_path: Path,
    repo_root: Path | None,
) -> _ChangedPathApplyResult:
    """Containment-safe deletion of a changed file under the admitted repo root.

    Uses ``O_NOFOLLOW`` directory-fd traversal: a parent directory (or the file
    itself) swapped to a link pointing outside the admitted root is rejected
    rather than dereferenced, so no external write/delete and no success event
    can follow an attacker/worker-swapped link.

    When no admitted ``repo_root`` is configured (the sealed-candidate effects
    exercised directly by recovery tests), the operation is scoped to the file's
    immediate parent directory, still with ``O_NOFOLLOW`` at every open so a
    swapped parent or file is rejected rather than followed.
    """
    if repo_root is not None and (relative := _target_relative(target_path, repo_root)) is not None:
        target_root = repo_root
    else:
        relative = target_path.name
        target_root = _strip_relative(target_path, relative).resolve()
    try:
        unlink_regular_nofollow(target_root, relative)
    except SourcePathError as exc:
        return _ChangedPathApplyResult.fail(
            "changed_file.delete_conflict",
            str(exc),
            str(target_path),
        )
    except OSError as exc:
        return _ChangedPathApplyResult.fail(
            "changed_file.delete_failed",
            f"Cannot delete changed file: {exc}",
            str(target_path),
        )
    return _ChangedPathApplyResult.ok()


def _promote_changed_nofollow(
    *,
    source_path: Path,
    target_path: Path,
    repo_root: Path | None,
    after_mode: object = None,
) -> _ChangedPathApplyResult:
    """Containment-safe promotion of a changed file into the admitted repo root.

    Handles a file/directory transition: when the target is currently an empty
    directory (leftover after child deletes) it is removed first; a non-empty
    directory is a conflict and is never removed recursively. Every parent
    directory and the final source/target file are opened with ``O_NOFOLLOW``, so
    a swapped parent link or file link unwrites/undeletes external bytes instead
    of dereferencing them.

    When no admitted ``repo_root`` is configured, the promotion is scoped to the
    file's immediate parent directory (the legacy single-file transport used by
    recovery tests), still ``O_NOFOLLOW`` at every open.
    """
    if repo_root is not None and (relative := _target_relative(target_path, repo_root)) is not None:
        target_root = repo_root
        target_relative = relative
        source_relative = relative
        source_root = _strip_relative(source_path, source_relative).resolve()
    else:
        target_relative = target_path.name
        target_root = _strip_relative(target_path, target_relative).resolve()
        source_relative = source_path.name
        source_root = _strip_relative(source_path, source_relative).resolve()
    mode_value = int(after_mode) if after_mode is not None else None
    try:
        # Directory -> file: remove an empty leftover directory first. A
        # non-empty directory blocks (an unrelated live file is preserved; the
        # directory is never removed recursively just to make promotion succeed).
        rmdir_if_empty_nofollow(target_root, target_relative)
        copy_regular_nofollow(
            source_root=source_root,
            source_relative=source_relative,
            target_root=target_root,
            target_relative=target_relative,
            mode=mode_value,
        )
    except SourcePathError as exc:
        return _ChangedPathApplyResult.fail(
            "changed_file.promotion_failed",
            str(exc),
            str(target_path),
        )
    except OSError as exc:
        return _ChangedPathApplyResult.fail(
            "changed_file.promotion_failed",
            f"Cannot promote changed file: {exc}",
            str(target_path),
        )
    return _ChangedPathApplyResult.ok()


def _file_sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    promoted_artifact_links: Mapping[tuple[str, str], str] | None = None,
    workflow: WorkflowDefinition | None = None,
) -> tuple[tuple[Mapping[str, object], ...], tuple[DomainError, ...]]:
    if transition.derives is None:
        return (), ()
    selected = tuple(artifact for artifact in artifacts if artifact.type == transition.derives.artifact_type)
    source_links = promoted_artifact_links or {}

    # Validate the entire batch before any task, board card, parent link, or
    # artifact is promoted. Persisted IDs are allocated only after every child
    # artifact validates, so a rejected batch leaves the tracker and board
    # untouched.
    parsed: list[tuple[str, str, tuple[str, ...], str, str, str | None]] = []
    errors: list[DomainError] = []
    local_ids: set[str] = set()
    seen_paths: set[str] = set()
    for artifact in selected:
        path = output_dir / artifact.path
        if artifact.path in seen_paths:
            errors.append(_error(
                "task.derived_duplicate_path",
                f"Duplicate derived task artifact path: {artifact.path}",
                artifact.path,
            ))
        seen_paths.add(artifact.path)
        try:
            local_id, title, dependencies, body = _parse_derived_task_file(path)
        except ValueError as exc:
            errors.append(_error("task.derived_invalid", str(exc), artifact.path))
            continue
        except OSError as exc:
            errors.append(_error(
                "task.derived_source_unreadable",
                f"Derived task source is not readable: {exc}",
                artifact.path,
            ))
            continue
        if local_id in local_ids:
            errors.append(_error(
                "task.derived_duplicate_local_id",
                f"Duplicate derived task local_id: {local_id}",
                local_id,
            ))
        local_ids.add(local_id)
        source_link = source_links.get((artifact.type, artifact.path))
        parsed.append((local_id, title, dependencies, body, artifact.path, source_link))
    if errors:
        return (), tuple(errors)

    existing_id_set = set(existing_task_ids)
    by_local_id = {item[0]: item for item in parsed}
    for local_id, _title, dependencies, _body, _path, _link in parsed:
        if local_id in existing_id_set:
            # A local_id colliding with a persisted task would fabricate a second
            # owner of existing work. Stop publication with a precise blocker.
            errors.append(_error(
                "task.derived_existing_prerequisite_collision",
                f"Derived local_id {local_id!r} collides with an existing task; "
                "existing unfinished work must be reused, not duplicated.",
                local_id,
            ))
        for dep in dependencies:
            if dep == local_id:
                errors.append(_error(
                    "task.derived_self_dependency",
                    f"Derived task {local_id!r} must not depend on itself.",
                    local_id,
                ))
            elif dep not in by_local_id:
                if dep in existing_id_set:
                    # The dependency names a persisted unfinished task, which the
                    # current derived-task format cannot express: local batch IDs
                    # are disjoint from persisted task IDs. Stop publication with
                    # a precise representation blocker rather than silently
                    # duplicating the existing work.
                    errors.append(_error(
                        "task.derived_existing_prerequisite_unrepresentable",
                        f"Derived task {local_id!r} depends on existing unfinished "
                        f"task {dep}, but the derived-task format can only express "
                        "dependencies on local batch IDs. Represent this prerequisite "
                        "by a supported persisted-ID reference or account for it "
                        "without duplicating existing work.",
                        local_id,
                    ))
                else:
                    errors.append(_error(
                        "task.derived_unknown_dependency",
                        f"Derived task {local_id!r} references unknown local dependency: {dep}",
                        local_id,
                    ))
    errors.extend(_derived_cycle_errors(tuple(item[0] for item in parsed), by_local_id))
    if errors:
        return (), tuple(errors)

    ids = _allocate_numeric_task_ids(tuple(item[0] for item in parsed), existing_task_ids)
    planned: list[Mapping[str, object]] = []
    for local_id, title, dependencies, body, _path, source_link in parsed:
        task_id = ids[local_id]
        task = Task(
            id=task_id,
            title=title,
            path=f"tasks/{task_id}.md",
            current_state=transition.derives.state,
            task_type=transition.derives.task_type,
            dependencies=tuple(ids[dep] for dep in dependencies),
            artifact_links=(source_link,) if source_link is not None else (),
            parent_id=parent_id,
            body=body,
        )
        if workflow is not None and task_uses_global_contract(task, workflow):
            errors.extend(validate_task_schema(body, location=_path))
        planned.append({"task": task, "link": f"{task_id}-{_slugify(title)}"})
    if errors:
        return (), tuple(errors)
    return tuple(planned), tuple(errors)


def _derived_cycle_errors(
    local_ids: tuple[str, ...],
    by_local_id: Mapping[str, tuple[str, str, tuple[str, ...], str, str, str | None]],
) -> tuple[DomainError, ...]:
    errors: list[DomainError] = []
    visited: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def _deps(node: str) -> tuple[str, ...]:
        return by_local_id[node][2]

    def _visit(node: str) -> None:
        visited.add(node)
        stack.append(node)
        on_stack.add(node)
        for dep in _deps(node):
            if dep not in by_local_id:
                continue
            if dep in on_stack:
                cycle = stack[stack.index(dep):] + [dep]
                errors.append(_error(
                    "task.derived_dependency_cycle",
                    f"Derived task dependency cycle detected: {' -> '.join(cycle)}",
                    node,
                ))
            elif dep not in visited:
                _visit(dep)
        stack.pop()
        on_stack.discard(node)

    for node in local_ids:
        if node not in visited:
            _visit(node)
    return tuple(errors)


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
    local_id_raw = parsed.get("local_id")
    if not isinstance(local_id_raw, str):
        raise ValueError("Derived task frontmatter requires local_id as a string.")
    local_id = local_id_raw.strip()
    if not local_id:
        raise ValueError("Derived task frontmatter requires a nonempty local_id.")
    dependencies_raw = parsed.get("dependencies")
    if dependencies_raw is None:
        dependencies = ()
    elif isinstance(dependencies_raw, str):
        dependencies = (dependencies_raw,)
    elif isinstance(dependencies_raw, list):
        if not all(isinstance(item, str) for item in dependencies_raw):
            raise ValueError("Derived task dependencies must be lists of strings.")
        dependencies = tuple(dependencies_raw)
    else:
        raise ValueError("Derived task dependencies must be a string or list.")
    title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
    if not title:
        raise ValueError("Derived task body requires a Markdown H1 title.")
    structure_errors = validate_task_structure(body, location=str(path))
    if structure_errors:
        raise ValueError(
            "Derived task body is structurally invalid: "
            + "; ".join(f"{error.code}: {error.message}" for error in structure_errors)
        )
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


_RECOVERABLE_EFFECT_TYPES = frozenset({
    "promote_artifact",
    "move_task",
    "create_task",
    "link_derived_tasks",
    "promote_changed_file",
    "delete_changed_file",
    "commit_repo_changes",
})


def recover_completion_transactions(
    *,
    service: CompletionService,
    event_store: JsonlEventStore,
    journal_store: TransactionJournalStore,
) -> tuple[str, ...]:
    """Roll a prepared completion journal forward without losing user work.

    For each prepared journal the transaction's own after-identity is inspected
    per effect: effects already applied are skipped, effects that can be proven
    safe to apply are rolled forward, and any intervening user change blocks the
    journal as an unresolved conflict while retaining the candidate/before images.
    A crash after a Git commit is detected and verified instead of producing a
    duplicate commit or blindly resetting branch history.
    """
    recovered: list[str] = []
    existing_event_ids = {event.event_id for event in event_store.iter_events()}
    for record in journal_store.iter_journals():
        effect_types = {effect.get("type") for effect in record.effects}
        if not effect_types or not effect_types.issubset(_RECOVERABLE_EFFECT_TYPES):
            continue
        if record.task_id is None or record.transition_id is None:
            continue
        is_committed = str(getattr(record.status, "value", record.status)) == "committed"
        expected_to_state = _expected_to_state(record)
        if not expected_to_state:
            continue
        candidate = None
        raw_candidate = record.context.get("candidate")
        if isinstance(raw_candidate, Mapping):
            try:
                payload = dict(raw_candidate)
                payload["changes"] = tuple(CandidateChange(**dict(change)) for change in payload["changes"])
                raw_selection = payload.get("source_selection")
                if isinstance(raw_selection, Mapping):
                    payload["source_selection"] = source_selection_from_dict(raw_selection)
                elif raw_selection is not None:
                    continue
                candidate = Candidate(**payload)
                if capture_deliverable_manifest(
                    Path(candidate.storage_path), candidate.source_selection
                ).sha256 != candidate.manifest_sha256:
                    continue
            except (OSError, TypeError, ValueError, KeyError):
                continue
        # An intervening user change anywhere in the intended change set must
        # stop recovery with the journal left prepared, never overwritten.
        if _recovery_has_conflict(service, record):
            continue
        if is_committed:
            # The journal/job crash window does not authorize replaying a
            # completed delivery or accepting a surface that has since drifted.
            identity_effects = {"create_task", "move_task", "promote_artifact",
                                "promote_changed_file", "delete_changed_file",
                                "commit_repo_changes"}
            if any(effect.get("type") in identity_effects
                   and not _recovery_effect_applied(service, record, effect)
                   for effect in record.effects):
                continue
            final = service._validate_final_state(
                record.task_id, expected_to_state, repo_root=service.repo_root,
                candidate=candidate, output_relative=record.context.get("output_relative"),
                artifact_paths=record.context.get("artifact_paths"),
            )
            if final.accepted and _settle_recovered_acceptance(service, record):
                recovered.append(record.journal_id)
            continue
        applying_ok = True
        for effect in record.effects:
            if _recovery_effect_applied(service, record, effect):
                continue
            result = service._apply_effect(effect)
            if not result.accepted:
                applying_ok = False
                break
        if not applying_ok:
            continue
        final = service._validate_final_state(
            record.task_id, expected_to_state, repo_root=service.repo_root,
            candidate=candidate, output_relative=record.context.get("output_relative"),
            artifact_paths=record.context.get("artifact_paths"),
        )
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
            _settle_recovered_acceptance(service, committed.record or record)
            recovered.append(record.journal_id)
    return tuple(recovered)


def _recovered_commit_identity(service: CompletionService, record) -> str | None:
    """The original accepted commit identity, proven, or None.

    For a commit-bearing record this is the head only when it is the exact
    intended transaction commit (tree, parent, and subject all match). It never
    binds the job to an unrelated tip that may have advanced during the crash
    window. When proof is unavailable the record stays unresolved.
    """
    commit_effect = next(
        (effect for effect in record.effects if effect.get("type") == "commit_repo_changes"),
        None,
    )
    if commit_effect is None:
        return None
    if service.repo_root is None:
        return None
    if not _transaction_commit_exists(service, record, commit_effect):
        return None
    runner = service.repo_command_runner or _run_repo_command
    return _git_rev(runner, service.repo_root, "HEAD")


def _settle_recovered_acceptance(service: CompletionService, record) -> bool:
    """Close the journal-commit/job-update crash window using saved evidence."""
    job_id = record.context.get("job_id")
    metadata = record.context.get("acceptance_metadata")
    if not isinstance(job_id, str) or not isinstance(metadata, Mapping):
        return False  # Historical journals do not invent missing acceptance.
    loaded = service.job_store.get(job_id)
    if not loaded.accepted or loaded.job is None:
        return False
    if _status(loaded.job.status) == ExecutionJobStatus.ACCEPTED.value:
        return False
    if loaded.job.task_id != record.task_id or loaded.job.transition_id != record.transition_id:
        return False
    restored = dict(metadata)
    has_commit_effect = any(effect.get("type") == "commit_repo_changes" for effect in record.effects)
    if has_commit_effect:
        commit_identity = _recovered_commit_identity(service, record)
        if commit_identity is None:
            # The original accepted commit cannot be proven (missing proof, wrong
            # tree, or an advanced unrelated tip). Never bind to whichever tip is
            # present; the journal remains unresolved for explicit handling.
            return False
        restored["acceptance_repository_commit"] = commit_identity
    elif record.context.get("repository_base_commit"):
        # No-op delivery records the original accepted source identity.
        restored["acceptance_repository_commit"] = record.context["repository_base_commit"]
    # Otherwise there is no repository identity to restore (non-Git/plain
    # acceptance); acceptance is restored from the saved acceptance metadata.
    return service.job_store.update_status(
        job_id, ExecutionJobStatus.ACCEPTED, metadata=restored,
    ).accepted


def _expected_to_state(record) -> str:
    return next(
        (
            str(effect.get("to_state", ""))
            for effect in record.effects
            if effect.get("type") == "move_task"
            and str(effect.get("task_id", "")) == record.task_id
        ),
        "",
    )


def _recovery_effect_applied(
    service: CompletionService,
    record,
    effect: Mapping[str, object],
) -> bool:
    """Whether the effect's after-identity is already present on disk/task."""
    kind = effect.get("type")
    if kind == "move_task":
        loaded = service.adapter.read_task(str(effect.get("task_id", "")))
        return (
            loaded.accepted
            and loaded.task is not None
            and loaded.task.current_state == str(effect.get("to_state", ""))
        )
    if kind == "create_task":
        payload = effect.get("task")
        if not isinstance(payload, Mapping):
            return False
        loaded = service.adapter.read_task(str(payload.get("id", "")))
        return (loaded.accepted and loaded.task is not None
                and _created_task_matches(loaded.task, payload))
    if kind in ("promote_artifact", "promote_changed_file"):
        source = Path(str(effect.get("source_path", "")))
        target = Path(str(effect.get("target_path", "")))
        if not source.is_file() or not target.is_file():
            return False
        return _same_file_content(source, target)
    if kind == "delete_changed_file":
        target = Path(str(effect.get("target_path", "")))
        return not target.exists()
    if kind == "commit_repo_changes":
        return _transaction_commit_exists(service, record, effect)
    return False


def _created_task_matches(task: Task, payload: Mapping[str, object]) -> bool:
    """Compare persisted task content while allowing adapter presentation changes."""
    try:
        expected = _task_from_mapping(payload)
    except (KeyError, TypeError, ValueError):
        return False

    def identity(value: Task) -> Mapping[str, object]:
        data = dict(_task_to_dict(value))
        # Adapters select the note filename and render the title as an H1.
        data.pop("path")
        lines = value.body.strip().splitlines()
        if lines and lines[0].strip().startswith("# "):
            lines = lines[1:]
        data["body"] = "\n".join(lines).strip()
        data["title"] = value.title.strip()
        reserved = {"id", "type", "state", "dependencies", "artifact_links", "parent_id"}
        data["metadata"] = {key: item for key, item in value.metadata.items() if key not in reserved}
        return data

    return identity(task) == identity(expected)


def _recovery_has_conflict(service: CompletionService, record) -> bool:
    """Detect an intervening user change that blocks rollback/roll-forward.

    A delete is admissible only when the file still matches the transaction's
    own expected before-image; anything else is a user change we must not
    overwrite. A Git commit effect whose branch tip is neither the recorded base
    nor the transaction's own commit is likewise a conflict. The journal is left
    prepared so the candidate/before images are retained for explicit conflict
    resolution.
    """
    for effect in record.effects:
        if effect.get("type") == "create_task":
            payload = effect.get("task")
            if not isinstance(payload, Mapping):
                return True
            loaded = service.adapter.read_task(str(payload.get("id", "")))
            if loaded.task is not None and not _created_task_matches(loaded.task, payload):
                return True
            if loaded.errors and any(error.code != "task.not_found" for error in loaded.errors):
                return True
        if effect.get("type") in {"promote_changed_file", "promote_artifact"}:
            source = Path(str(effect.get("source_path", "")))
            target = Path(str(effect.get("target_path", "")))
            expected_after = effect.get("expected_after_sha256")
            if not source.is_file():
                return True
            if isinstance(expected_after, str) and _file_sha256(source) != expected_after:
                return True
            if target.is_file() and _same_file_content(source, target):
                continue
            expected_before = effect.get("expected_before_sha256")
            if target.exists():
                if target.is_symlink():
                    # A swapped link is never an admissible promote target.
                    return True
                if target.is_dir():
                    # directory -> file transition: an empty leftover directory
                    # is the expected intermediate state recovery must complete;
                    # a non-empty directory holds an unrelated live file -> conflict.
                    try:
                        next(target.iterdir())
                    except StopIteration:
                        continue
                    return True
                if not target.is_file() or not isinstance(expected_before, str):
                    return True
                if _file_sha256(target) != expected_before:
                    return True
                before_mode = effect.get("expected_before_mode")
                if before_mode is not None and target.stat().st_mode & 0o777 != before_mode:
                    return True
            elif expected_before is not None or effect.get("target_existed"):
                return True
        if effect.get("type") == "move_task":
            loaded = service.adapter.read_task(str(effect.get("task_id", "")))
            previous = record.context.get("expected_previous_state")
            if previous is not None and (not loaded.accepted or loaded.task is None or
                    loaded.task.current_state not in {previous, effect.get("to_state")}):
                return True
        if effect.get("type") == "delete_changed_file":
            target = Path(str(effect.get("target_path", "")))
            expected_before = effect.get("expected_before_sha256")
            if target.exists() and isinstance(expected_before, str):
                if _file_sha256(target) != expected_before:
                    return True
        if effect.get("type") == "commit_repo_changes":
            if _transaction_commit_exists(service, record, effect):
                continue
            base = str(record.context.get("repository_base_commit", ""))
            runner = service.repo_command_runner or _run_repo_command
            if service.repo_root is not None and base:
                head = _git_rev(runner, service.repo_root, "HEAD")
                if head and head != base:
                    return True
    return False


def _transaction_commit_exists(
    service: CompletionService,
    record,
    effect: Mapping[str, object],
) -> bool:
    """Locate and verify the already-created transaction commit.

    Returns True only when the branch tip's full object identities match the
    intended parent, tree, and subject. A matching subject alone is never
    sufficient: the committed tree must equal the intended tree persisted before
    the commit effect. A different tip is an unresolved conflict, and a record
    without an unambiguous intended tree cannot prove success (R4).
    """
    if service.repo_root is None:
        return False
    runner = service.repo_command_runner or _run_repo_command
    commit_ctx = record.context.get("commit")
    if not isinstance(commit_ctx, Mapping):
        # No committed-tree identity was persisted (historical/unresolved).
        return False
    intended_tree = commit_ctx.get("intended_tree")
    if not isinstance(intended_tree, str):
        return False
    message = str(effect.get("message", ""))
    base_ref = commit_ctx.get("repository_base_commit") or record.context.get("repository_base_commit")
    head = _git_rev(runner, service.repo_root, "HEAD")
    if not head:
        return False
    if base_ref:
        base = _git_rev(runner, service.repo_root, str(base_ref))
        if not base:
            # Missing/unambiguous-object lookup must not infer success.
            return False
        if head == base:
            return False
        parent = _git_parent(runner, service.repo_root, head)
        if not parent or parent != base:
            return False
    tree = _git_tree(runner, service.repo_root, head)
    if tree != intended_tree:
        return False
    if message and _git_show(runner, service.repo_root, head) != message:
        return False
    return True


def _git_rev(runner, repo_root: Path, ref: str) -> str | None:
    """Resolve a reference to an unambiguous full object id.

    ``rev-parse --verify`` rejects an ambiguous short name, a missing object, or
    an unreadable lookup, so success is never inferred from a partial identity.
    """
    result = runner(("git", "rev-parse", "--verify", ref), repo_root)
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _git_parent(runner, repo_root: Path, commit: str) -> str | None:
    result = runner(("git", "rev-parse", "--verify", f"{commit}^"), repo_root)
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _git_tree(runner, repo_root: Path, commit: str) -> str | None:
    result = runner(("git", "rev-parse", "--verify", f"{commit}^{{tree}}"), repo_root)
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _git_tree_mode(mode: object) -> str:
    """Git cacheinfo mode for a regular file from a POSIX mode.

    Executable bits select ``100755``; everything else is ``100644``.
    """
    if mode is None:
        return "100644"
    value = int(mode)
    return "100755" if (value & 0o111) else "100644"


def _intended_commit_tree(
    *,
    repo_root: Path | None,
    base: str | None,
    effects: Sequence[Mapping[str, object]],
) -> str | None:
    """Compute the git tree identity the transaction commit must have.

    Returns None (not provable) when the repository or any candidate blob cannot
    be read, so acceptance never rests on a partial identity. Uses a private
    temporary index so the worker's own index/working tree is never mutated: the
    baseline tree is populated, candidate blob content is injected with
    ``--cacheinfo``, deletions are removed, and ``write-tree`` yields the tree.
    """
    if repo_root is None:
        return None
    repo_root = repo_root.resolve()
    changes = [
        effect for effect in effects
        if effect.get("type") in ("promote_changed_file", "delete_changed_file")
    ]
    with tempfile.TemporaryDirectory() as td:
        index = Path(td) / "index"
        index.write_bytes(b"")
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index)

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ("git", *args),
                cwd=str(repo_root),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        if base and git("read-tree", base).returncode != 0:
            return None
        for change in changes:
            kind = change.get("type")
            if kind == "promote_changed_file":
                source = Path(str(change.get("source_path", "")))
                target = Path(str(change.get("target_path", "")))
                try:
                    relative = str(target.relative_to(repo_root))
                except ValueError:
                    continue
                hashed = git("hash-object", "-w", str(source))
                if hashed.returncode != 0 or not (hashed.stdout or "").strip():
                    return None
                blob = hashed.stdout.strip()
                git_mode = _git_tree_mode(change.get("expected_after_mode"))
                if (
                    git(
                        "update-index", "--cacheinfo",
                        f"{git_mode},{blob},{relative}",
                    ).returncode
                    != 0
                ):
                    return None
            elif kind == "delete_changed_file":
                target = Path(str(change.get("target_path", "")))
                try:
                    relative = str(target.relative_to(repo_root))
                except ValueError:
                    continue
                git("update-index", "--force-remove", relative)
        tree = git("write-tree")
        if tree.returncode != 0 or not (tree.stdout or "").strip():
            return None
        return tree.stdout.strip()


def _git_show(runner, repo_root: Path, commit: str) -> str | None:
    result = runner(("git", "log", "-1", "--pretty=%s", commit), repo_root)
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()
