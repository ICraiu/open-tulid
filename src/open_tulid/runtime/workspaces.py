from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from open_tulid.domain import DomainError, ExecutionJob, Task, TransitionDefinition
from open_tulid.runtime.context import sanitize_task_body_for_runtime
from open_tulid.runtime.execution_contracts import (
    ExecutionContract,
    execution_contract_to_dict,
    load_job_execution_contract,
)
from open_tulid.runtime.repository_facts import (
    EXCLUDED_DIRECTORY_NAMES,
    baseline_manifest_to_dict,
    capture_repository_snapshot,
    repository_facts_to_dict,
)
from open_tulid.runtime.task_contracts import task_source_intent_sha256


@dataclass(frozen=True)
class WorkspacePrepareResult:
    workspace: Path | None = None
    output_dir: Path | None = None
    error: DomainError | None = None

    @property
    def accepted(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    removed: tuple[Path, ...] = ()
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


class WorkspacePreparer:
    def __init__(self, *, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root

    def prepare(
        self,
        *,
        job: ExecutionJob,
        task: Task,
        transition: TransitionDefinition,
        completion_endpoint: str | None = None,
        preserve_workspace: bool = False,
    ) -> WorkspacePrepareResult:
        workspace = Path(job.workspace_path)
        output_dir = workspace / "output"
        frozen = load_job_execution_contract(job)
        if not frozen.accepted:
            return WorkspacePrepareResult(error=frozen.errors[0])
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            if self.repo_root is not None and not preserve_workspace:
                if not self.repo_root.is_dir():
                    return WorkspacePrepareResult(error=DomainError(
                        code="workspace.repo_missing",
                        message=f"Repository root does not exist: {self.repo_root}",
                        location=str(self.repo_root),
                    ))
                _copy_repo(self.repo_root, workspace)
            if frozen.contract is not None and not preserve_workspace:
                copied = capture_repository_snapshot(workspace)
                if not copied.accepted or copied.snapshot is None:
                    return WorkspacePrepareResult(error=(
                        copied.errors[0]
                        if copied.errors
                        else DomainError(
                            code="workspace.baseline_capture_failed",
                            message="Cannot capture the prepared workspace baseline.",
                            location=str(workspace),
                        )
                    ))
                if (
                    copied.snapshot.baseline.sha256
                    != frozen.contract.baseline_manifest.sha256
                ):
                    return WorkspacePrepareResult(error=DomainError(
                        code="workspace.baseline_mismatch",
                        message=(
                            "Repository contents changed after this job's execution "
                            "contract was frozen."
                        ),
                        location=str(workspace),
                    ))
            _write_context(
                workspace=workspace,
                job=job,
                task=task,
                transition=transition,
                output_dir=output_dir,
                completion_endpoint=completion_endpoint,
                execution_contract=frozen.contract,
            )
        except OSError as exc:
            return WorkspacePrepareResult(error=DomainError(
                code="workspace.prepare_failed",
                message=f"Cannot prepare workspace: {exc}",
                location=str(workspace),
            ))
        return WorkspacePrepareResult(workspace=workspace, output_dir=output_dir)


def cleanup_job_workspaces(jobs: tuple[ExecutionJob, ...]) -> WorkspaceCleanupResult:
    removable_statuses = {"accepted", "failed", "stale", "cancelled"}
    removed: list[Path] = []
    errors: list[DomainError] = []
    for job in jobs:
        status = job.status.value if hasattr(job.status, "value") else str(job.status)
        if status not in removable_statuses:
            continue
        workspace = Path(job.workspace_path)
        if not workspace.exists():
            continue
        try:
            shutil.rmtree(workspace)
            removed.append(workspace)
        except OSError as exc:
            errors.append(DomainError(
                code="workspace.cleanup_failed",
                message=f"Cannot remove workspace: {exc}",
                location=str(workspace),
            ))
    return WorkspaceCleanupResult(removed=tuple(removed), errors=tuple(errors))


def _copy_repo(source: Path, target: Path) -> None:
    for child in source.iterdir():
        if child.name in EXCLUDED_DIRECTORY_NAMES:
            continue
        destination = target / child.name
        if child.is_dir():
            shutil.copytree(
                child,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*EXCLUDED_DIRECTORY_NAMES),
            )
        else:
            shutil.copy2(child, destination)


def _write_context(
    *,
    workspace: Path,
    job: ExecutionJob,
    task: Task,
    transition: TransitionDefinition,
    output_dir: Path,
    completion_endpoint: str | None,
    execution_contract: ExecutionContract | None,
) -> None:
    context_dir = workspace / ".open-tulid"
    context_dir.mkdir(parents=True, exist_ok=True)
    payload: Mapping[str, object] = {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "task_id": job.task_id,
        "transition_id": job.transition_id,
        "worker_id": job.worker_id,
        "from_state": transition.from_state,
        "to_state": transition.to_state,
        "required_artifacts": list(transition.requires.artifacts),
        "required_validations": [call.type for call in transition.requires.validations],
        "completion_endpoint": completion_endpoint,
        "workspace_path": str(workspace),
        "output_path": str(output_dir),
        "container_output_path": "/workspace/project/output",
        "source_intent_sha256": task_source_intent_sha256(task),
        "execution_contract_sha256": (
            execution_contract.sha256
            if execution_contract is not None
            else None
        ),
        "execution_contract_path": (
            ".open-tulid/execution-contract.json"
            if execution_contract is not None
            else None
        ),
        "repository_facts_path": (
            ".open-tulid/repository-facts.json"
            if execution_contract is not None
            else None
        ),
        "baseline_manifest_path": (
            ".open-tulid/baseline-manifest.json"
            if execution_contract is not None
            else None
        ),
        "task": {
            "id": task.id,
            "title": task.title,
            "path": task.path,
            "type": task.task_type,
            "state": task.current_state,
            "dependencies": list(task.dependencies),
            "artifact_links": list(task.artifact_links),
            "parent_id": task.parent_id,
            "metadata": dict(task.metadata),
            "body": sanitize_task_body_for_runtime(task.body),
        },
    }
    (context_dir / "job-context.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if execution_contract is not None:
        _write_json(
            context_dir / "execution-contract.json",
            execution_contract_to_dict(execution_contract),
        )
        _write_json(
            context_dir / "repository-facts.json",
            repository_facts_to_dict(execution_contract.repository_facts),
        )
        _write_json(
            context_dir / "baseline-manifest.json",
            baseline_manifest_to_dict(execution_contract.baseline_manifest),
        )


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
