from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import (
    DomainError,
    EventActor,
    EventEnvelope,
    EventType,
    ExecutionJob,
    ExecutionJobStatus,
    ProjectSnapshot,
    WorkflowDefinition,
)
from open_tulid.domain.completion import SUCCESS, terminal_outcome_of

from .events import build_event, new_ulid, TransactionJournalStore
from .context import load_parent_tasks
from .execution_contracts import (
    compile_standard_execution_contract,
    compile_task_execution_contract,
    execution_contract_to_dict,
)
from .prompts import (
    PromptBudgetError,
    compile_execution_prompt,
    find_review_evidence,
    is_review_transition,
)
from .task_contracts import (
    implementation_contract_required,
    task_uses_global_contract,
)
from .repository_facts import repository_identity
from open_tulid.vault.task_schema import validate_task_structure


@dataclass(frozen=True)
class RequestTransition:
    project_id: str
    task_id: str
    transition_id: str
    actor: EventActor = field(default_factory=lambda: EventActor(type="system", id="task-manager"))


@dataclass(frozen=True)
class ValidateProject:
    project_id: str


@dataclass(frozen=True)
class CreateExecutionJob:
    project_id: str
    task_id: str
    transition_id: str
    workspace_root: Path
    job_id: str | None = None
    actor: EventActor = field(default_factory=lambda: EventActor(type="system", id="task-manager"))


@dataclass(frozen=True)
class RecordExecutionResult:
    project_id: str
    job_id: str
    accepted: bool
    message: str = ""
    data: Mapping[str, object] = field(default_factory=dict)
    actor: EventActor = field(default_factory=lambda: EventActor(type="system", id="task-manager"))


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    errors: tuple[DomainError, ...] = ()
    events: tuple[EventEnvelope, ...] = ()
    effects: tuple[Mapping[str, object], ...] = ()
    job: ExecutionJob | None = None


class TaskManager:
    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        adapter: StorageAdapter,
        job_store: object | None = None,
        history_job_store: object | None = None,
        project_root: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.workflow = workflow
        self.adapter = adapter
        self.job_store = job_store
        self.history_job_store = history_job_store or job_store
        self.project_root = project_root
        self.repo_root = repo_root

    def handle(
        self,
        command: RequestTransition | ValidateProject | CreateExecutionJob | RecordExecutionResult,
    ) -> CommandResult:
        if isinstance(command, ValidateProject):
            return self.validate_project(command)
        if isinstance(command, RequestTransition):
            return self.request_transition(command)
        if isinstance(command, CreateExecutionJob):
            return self.create_execution_job(command)
        if isinstance(command, RecordExecutionResult):
            return self.record_execution_result(command)
        return CommandResult(accepted=False, errors=(_error(
            "command.unsupported",
            f"Unsupported command: {type(command).__name__}",
        ),))

    def validate_project(self, command: ValidateProject) -> CommandResult:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return CommandResult(accepted=False, errors=loaded.errors)
        snapshot = loaded.snapshot
        if snapshot is None:
            return CommandResult(accepted=False, errors=(_error(
                "project.snapshot_missing",
                "Adapter returned no project snapshot.",
            ),))
        errors = _validate_snapshot_against_workflow(snapshot, self.workflow)
        return CommandResult(accepted=not errors, errors=tuple(errors))

    def request_transition(self, command: RequestTransition) -> CommandResult:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return CommandResult(accepted=False, errors=loaded.errors)
        snapshot = loaded.snapshot
        if snapshot is None:
            return CommandResult(accepted=False, errors=(_error(
                "project.snapshot_missing",
                "Adapter returned no project snapshot.",
            ),))
        task = snapshot.tasks.get(command.task_id)
        if task is None:
            return CommandResult(accepted=False, errors=(_error(
                "task.not_found",
                f"Task {command.task_id!r} was not found.",
                command.task_id,
            ),))
        transition = self.workflow.transitions.get(command.transition_id)
        if transition is None:
            return CommandResult(accepted=False, errors=(_error(
                "transition.not_found",
                f"Transition {command.transition_id!r} is not defined.",
                command.transition_id,
            ),))
        if transition.task_type != task.task_type:
            return CommandResult(accepted=False, errors=(_error(
                "transition.task_type_mismatch",
                f"Transition {transition.id!r} expects task type {transition.task_type!r}.",
                task.id,
            ),))
        if transition.from_state != task.current_state:
            return CommandResult(accepted=False, errors=(_error(
                "transition.state_mismatch",
                f"Transition {transition.id!r} requires state {transition.from_state!r}.",
                task.id,
            ),))
        requirement_errors = _validate_task_state_requirements(task, transition.to_state, self.workflow)
        if requirement_errors:
            return CommandResult(accepted=False, errors=tuple(requirement_errors))
        acceptance_error = _manual_implementation_acceptance_error(
            self.workflow,
            transition,
            history_job_store=self.history_job_store,
            project_id=command.project_id,
            task_id=task.id,
            task=task,
            project_root=self.project_root,
            adapter=self.adapter,
            repo_root=self.repo_root,
        )
        if acceptance_error is not None:
            return CommandResult(accepted=False, errors=(acceptance_error,))
        events = (_event(
            project_id=command.project_id,
            actor=command.actor,
            event_type=EventType.TransitionAccepted,
            task_id=task.id,
            transition_id=transition.id,
            data={"from": transition.from_state, "to": transition.to_state},
        ), _event(
            project_id=command.project_id,
            actor=command.actor,
            event_type=EventType.TaskMoved,
            task_id=task.id,
            transition_id=transition.id,
            data={"from": transition.from_state, "to": transition.to_state},
        ))
        effects = ({
            "type": "move_task",
            "task_id": task.id,
            "from_state": transition.from_state,
            "to_state": transition.to_state,
        },)
        return CommandResult(accepted=True, events=events, effects=effects)

    def create_execution_job(self, command: CreateExecutionJob) -> CommandResult:
        loaded = self.adapter.load_project()
        if not loaded.accepted:
            return CommandResult(accepted=False, errors=loaded.errors)
        snapshot = loaded.snapshot
        if snapshot is None:
            return CommandResult(accepted=False, errors=(_error(
                "project.snapshot_missing",
                "Adapter returned no project snapshot.",
            ),))
        task = snapshot.tasks.get(command.task_id)
        if task is None:
            return CommandResult(accepted=False, errors=(_error(
                "task.not_found",
                f"Task {command.task_id!r} was not found.",
                command.task_id,
            ),))
        structure_errors = validate_task_structure(task.body, location=task.path, require_title=False)
        if structure_errors:
            return CommandResult(accepted=False, errors=tuple(structure_errors))
        transition = self.workflow.transitions.get(command.transition_id)
        if transition is None:
            return CommandResult(accepted=False, errors=(_error(
                "transition.not_found",
                f"Transition {command.transition_id!r} is not defined.",
                command.transition_id,
            ),))
        if transition.task_type != task.task_type:
            return CommandResult(accepted=False, errors=(_error(
                "transition.task_type_mismatch",
                f"Transition {transition.id!r} expects task type {transition.task_type!r}.",
                task.id,
            ),))
        if transition.from_state != task.current_state:
            return CommandResult(accepted=False, errors=(_error(
                "transition.state_mismatch",
                f"Transition {transition.id!r} requires state {transition.from_state!r}.",
                task.id,
            ),))
        if transition.worker is None:
            return CommandResult(accepted=False, errors=(_error(
                "transition.worker_missing",
                f"Transition {transition.id!r} has no worker.",
                transition.id,
            ),))
        frozen_contract = None
        compiled_prompt = None
        planning_metadata = {}
        job_id = command.job_id or new_ulid()
        lineage_errors = _validate_parent_lineage(self.adapter, task)
        if lineage_errors:
            return CommandResult(accepted=False, errors=tuple(lineage_errors))
        uses_global_contract = task_uses_global_contract(task, self.workflow)
        if self.project_root is None:
            if uses_global_contract:
                return CommandResult(accepted=False, errors=(_error(
                    "execution_contract.project_root_missing",
                    "Global-contract execution requires a project tracker root.",
                    task.id,
                ),))
        elif uses_global_contract:
            # Primary path: compile from the project global contract (contract.yaml)
            # plus acceptance.yaml. No per-task LLM-authored contract is needed.
            lineage_errors = _validate_parent_lineage(self.adapter, task)
            if lineage_errors:
                return CommandResult(accepted=False, errors=tuple(lineage_errors))
            compiled = compile_standard_execution_contract(
                project_root=self.project_root,
                repo_root=self.repo_root,
                task=task,
                transition=transition,
                parent_tasks=load_parent_tasks(self.adapter, task),
            )
            if not compiled.accepted or compiled.contract is None:
                return CommandResult(accepted=False, errors=compiled.errors)
            frozen_contract = compiled.contract
        elif implementation_contract_required(task, self.workflow):
            # Backward-compatible legacy path: a workflow still requires a
            # per-task ImplementationContract artifact. Kept only for historical
            # and migrated projects; new workflows never take this branch.
            compiled = compile_task_execution_contract(
                project_root=self.project_root,
                repo_root=self.repo_root,
                task=task,
                transition=transition,
            )
            if not compiled.accepted or compiled.contract is None:
                return CommandResult(accepted=False, errors=compiled.errors)
            frozen_contract = compiled.contract
        if frozen_contract is not None:
            review_evidence = None
            if is_review_transition(transition):
                if self.history_job_store is None:
                    return CommandResult(accepted=False, errors=(_error(
                        "prompt.review_evidence_missing",
                        "Self-review requires the immutable prior implementation result.",
                        task.id,
                    ),))
                listed = self.history_job_store.list()
                if not listed.accepted:
                    return CommandResult(accepted=False, errors=(listed.error or _error(
                        "job.read_failed",
                        "Cannot inspect prior implementation jobs.",
                        task.id,
                    ),))
                review_evidence = find_review_evidence(
                    listed.jobs,
                    project_id=command.project_id,
                    task_id=task.id,
                    review_transition=transition,
                    current_contract=frozen_contract,
                    repo_root=self.repo_root,
                    workflow=self.workflow,
                    journals=TransactionJournalStore(
                        self.project_root / "events" / "journals"
                    ),
                )
                if review_evidence is None:
                    return CommandResult(accepted=False, errors=(_error(
                        "prompt.review_evidence_missing",
                        "No accepted implementation verification report is available for self-review.",
                        task.id,
                    ),))
            try:
                compiled_prompt = compile_execution_prompt(
                    frozen_contract,
                    review_evidence=review_evidence,
                )
            except PromptBudgetError as exc:
                return CommandResult(accepted=False, errors=(_error(
                    exc.code,
                    str(exc),
                    task.id,
                ),))
            except ValueError as exc:
                return CommandResult(accepted=False, errors=(_error(
                    "prompt.compile_failed",
                    str(exc),
                    task.id,
                ),))
        if frozen_contract is None:
            from .executor import render_execution_prompt
            from .planning_inputs import freeze_planning_inputs
            rendered = render_execution_prompt(
                workflow=self.workflow, adapter=self.adapter, task=task,
                transition=transition, worker_id=transition.worker,
                job_id=job_id, completion_endpoint="", project_root=self.project_root,
            )
            if not rendered.accepted:
                return CommandResult(accepted=False, errors=rendered.errors)
            planning_metadata = {"planning_inputs": freeze_planning_inputs(
                task, transition, rendered.text, rendered.context_files,
            )}
        workspace = command.workspace_root / job_id
        output_path = workspace / "output"
        job = ExecutionJob(
            job_id=job_id,
            project_id=command.project_id,
            task_id=command.task_id,
            transition_id=command.transition_id,
            worker_id=transition.worker,
            workspace_path=str(workspace),
            metadata={
                **planning_metadata,
                "completion_token": secrets.token_urlsafe(24),
                "output_path": str(output_path),
                **(
                    {"repository_identity": repository_identity(self.repo_root)}
                    if self.repo_root is not None else {}
                ),
                **(
                    {
                        "execution_contract": execution_contract_to_dict(frozen_contract),
                        "execution_contract_sha256": frozen_contract.sha256,
                        "prompt_packet": compiled_prompt.text,
                        "prompt_packet_sha256": compiled_prompt.manifest.packet_sha256,
                        "prompt_manifest": compiled_prompt.manifest.to_dict(),
                        **(
                            {"review_source_job_id": review_evidence.source_job_id}
                            if review_evidence is not None else {}
                        ),
                    }
                    if frozen_contract is not None
                    else {}
                ),
            },
        )
        if self.job_store is not None:
            saved = self.job_store.create(job)
            if not saved.accepted:
                return CommandResult(
                    accepted=False,
                    errors=(saved.error or _error("job.write_failed", "Execution job was not recorded."),),
                )
            if saved.job is not None:
                job = saved.job
        events = (
            _event(
                project_id=command.project_id,
                actor=command.actor,
                event_type=EventType.ExecutionJobCreated,
                task_id=command.task_id,
                transition_id=command.transition_id,
                job_id=job.job_id,
                data={
                    "worker_id": job.worker_id,
                    "workspace_path": job.workspace_path,
                    **(
                        {"execution_contract_sha256": frozen_contract.sha256}
                        if frozen_contract is not None
                        else {}
                    ),
                },
            ),
        )
        effects = ({"type": "create_execution_job", "job": _job_to_dict(job)},)
        return CommandResult(accepted=True, events=events, effects=effects, job=job)

    def record_execution_result(self, command: RecordExecutionResult) -> CommandResult:
        event_type = EventType.ExecutionFinished if command.accepted else EventType.ExecutionFailed
        return CommandResult(accepted=True, events=(_event(
            project_id=command.project_id,
            actor=command.actor,
            event_type=event_type,
            job_id=command.job_id,
            data={"message": command.message, **dict(command.data)},
        ),))


def _validate_snapshot_against_workflow(
    snapshot: ProjectSnapshot,
    workflow: WorkflowDefinition,
) -> list[DomainError]:
    errors: list[DomainError] = []
    for task in snapshot.tasks.values():
        if task.task_type not in workflow.task_types:
            errors.append(_error(
                "task.unknown_type",
                f"Task {task.id!r} uses unknown task type {task.task_type!r}.",
                task.id,
            ))
        if task.current_state not in workflow.states:
            errors.append(_error(
                "task.unknown_state",
                f"Task {task.id!r} is in unknown state {task.current_state!r}.",
                task.id,
            ))
        if task.task_type in workflow.task_types and task.current_state in workflow.states:
            errors.extend(_validate_task_state_requirements(task, task.current_state, workflow))
    return errors


def _validate_task_state_requirements(
    task,
    state: str,
    workflow: WorkflowDefinition,
) -> list[DomainError]:
    """Validate requirements observable from a persisted Task snapshot.

    Artifact links are the only requirement evidence currently carried by the
    task model. Validation calls and changed-file evidence are execution-time
    concepts, so this deliberately leaves them to the verifier.
    """
    task_type = workflow.task_types.get(task.task_type)
    if task_type is None:
        return []
    requirements = task_type.requirements_by_state.get(state)
    if requirements is None:
        return []
    errors: list[DomainError] = []
    for artifact_type in requirements.artifacts:
        if not _has_artifact_link(task.artifact_links, artifact_type):
            errors.append(_error(
                "task.required_artifact_missing",
                f"Task {task.id!r} in state {state!r} requires artifact {artifact_type!r}.",
                task.id,
            ))
    return errors


def _manual_implementation_acceptance_error(
    workflow: WorkflowDefinition,
    transition,
    *,
    history_job_store,
    project_id: str,
    task_id: str,
    task,
    project_root,
    adapter,
    repo_root=None,
) -> DomainError | None:
    """Align a manual request with the runtime acceptance path.

    A manual request that would move a task into a state declaring a *success*
    terminal outcome is an implementation-completing transition: it claims the
    same verified completion the runtime records. Such a request must carry the
    same committed acceptance record (an accepted job whose delivery transaction
    was durably committed) that the automatic path requires. Absent that
    evidence, the request is rejected with an explanation and the code/task/board
    state are left intact. Manual transitions into non-success-terminal states
    (clarification, business decisions, block/cancel) intentionally carry no
    implementation-success guarantee and remain available.
    """
    if terminal_outcome_of(workflow, transition.to_state) != SUCCESS:
        return None
    if transition.worker is None and not task_uses_global_contract(task, workflow):
        return None
    if history_job_store is None:
        return _error(
            "manual.implementation_acceptance_unverifiable",
            (
                f"Manual transition {transition.id!r} would move task {task_id!r} "
                f"to success state {transition.to_state!r}, which requires a verified "
                "implementation with committed delivery, but no recorded job store is "
                "available to confirm that evidence. Refusing to fabricate verified success; "
                "the code/task/board state is left intact."
            ),
            task_id,
        )
    listed = history_job_store.list()
    if not listed.accepted:
        return _error(
            "manual.implementation_acceptance_unreadable",
            f"Manual transition {transition.id!r} cannot confirm acceptance evidence.",
            task_id,
        )
    from .acceptance import accepted_task_evidence
    from .events import TransactionJournalStore
    from .context import resolve_source_content_identities
    journals = TransactionJournalStore(project_root / "events" / "journals") if project_root else None
    sources = resolve_source_content_identities(
        project_root=project_root, task=task, transition=transition,
        parent_tasks=load_parent_tasks(adapter, task),
    ) if project_root else ()
    accepted = tuple(
        job for job in listed.jobs
        if job.project_id == project_id
        and job.task_id == task_id
        and job.transition_id == transition.id
        and _job_status_value(job.status) == ExecutionJobStatus.ACCEPTED.value
        and job.metadata.get("acceptance_transaction_id")
        and accepted_task_evidence(job, task=task, workflow=workflow, journals=journals,
            source_identities=sources, target_state=transition.to_state, repo_root=repo_root)
    )
    if accepted:
        return None
    return _error(
        "manual.implementation_require_acceptance",
        (
            f"Manual transition {transition.id!r} would move task {task_id!r} to "
            f"success state {transition.to_state!r}, which requires a verified "
            "implementation with committed delivery. No accepted verification/delivery "
            "record exists for this task and transition. The code/task/board state is "
            "left intact; run the scheduled implementation so it can be verified and "
            "delivered before moving the card."
        ),
        task_id,
    )


def _job_status_value(status: ExecutionJobStatus | str) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _has_artifact_link(links: tuple[str, ...], artifact_type: str) -> bool:
    # Completion-promoted links are shaped like artifacts/<task>/<type>/<file>.
    # Accept bare type links too, since adapters may preserve user-authored links.
    return any(artifact_type in Path(link).parts or link == artifact_type for link in links)


def _event(
    *,
    project_id: str,
    actor: EventActor,
    event_type: EventType,
    task_id: str | None = None,
    transition_id: str | None = None,
    job_id: str | None = None,
    data: Mapping[str, object] | None = None,
) -> EventEnvelope:
    return build_event(
        project_id=project_id,
        actor=actor,
        event_type=event_type,
        correlation_id=new_ulid(),
        task_id=task_id,
        transition_id=transition_id,
        job_id=job_id,
        data=data,
    )


def _validate_parent_lineage(adapter: StorageAdapter, task: Task) -> list[DomainError]:
    """Detect a parent cycle before a job is admitted.

    ``load_parent_tasks`` bounds the walk defensively, but a genuine cycle must
    be surfaced as a blocking diagnostic rather than silently truncated context.
    """
    seen: set[str] = set()
    current_id = task.parent_id
    while current_id:
        if current_id in seen:
            return [_error(
                "task.parent_cycle",
                (
                    f"Task {task.id!r} has a cyclic parent lineage at "
                    f"{current_id!r}; parent context cannot be resolved."
                ),
                task.id,
            )]
        seen.add(current_id)
        loaded = adapter.read_task(current_id)
        if not loaded.accepted or loaded.task is None:
            break
        current_id = loaded.task.parent_id
    return []


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _job_to_dict(job: ExecutionJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "task_id": job.task_id,
        "transition_id": job.transition_id,
        "worker_id": job.worker_id,
        "workspace_path": job.workspace_path,
        "status": str(job.status.value if hasattr(job.status, "value") else job.status),
        "attempts": job.attempts,
        "metadata": dict(job.metadata),
    }
