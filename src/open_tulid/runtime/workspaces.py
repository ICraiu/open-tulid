from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from open_tulid.domain import DomainError, ExecutionJob, Task, TransitionDefinition
from open_tulid.runtime.context import sanitize_task_body_for_runtime


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
    ) -> WorkspacePrepareResult:
        workspace = Path(job.workspace_path)
        output_dir = workspace / "output"
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            if self.repo_root is not None:
                if not self.repo_root.is_dir():
                    return WorkspacePrepareResult(error=DomainError(
                        code="workspace.repo_missing",
                        message=f"Repository root does not exist: {self.repo_root}",
                        location=str(self.repo_root),
                    ))
                _copy_repo(self.repo_root, workspace)
            _write_context(
                workspace=workspace,
                job=job,
                task=task,
                transition=transition,
                output_dir=output_dir,
                completion_endpoint=completion_endpoint,
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
        if child.name in {".git", ".open-tulid", "__pycache__"}:
            continue
        destination = target / child.name
        if child.is_dir():
            shutil.copytree(
                child,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"),
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
        "task": {
            "id": task.id,
            "title": task.title,
            "path": task.path,
            "type": task.task_type,
            "state": task.current_state,
            "dependencies": list(task.dependencies),
            "body": sanitize_task_body_for_runtime(task.body),
        },
    }
    (context_dir / "job-context.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
