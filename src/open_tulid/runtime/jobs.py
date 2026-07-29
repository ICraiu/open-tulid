from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from open_tulid.domain import DomainError, ExecutionJob, ExecutionJobStatus
from open_tulid.runtime.events import utc_now


ACTIVE_JOB_STATUSES = frozenset({
    ExecutionJobStatus.PENDING.value,
    ExecutionJobStatus.RUNNING.value,
    ExecutionJobStatus.COMPLETION_SUBMITTED.value,
    ExecutionJobStatus.COMPLETION_REJECTED.value,
    # A stale job needs explicit reconciliation before the same transition is
    # admitted again; otherwise a dead runtime can silently fork work.
    ExecutionJobStatus.STALE.value,
})

IMMUTABLE_JOB_METADATA_KEYS = frozenset({
    "execution_contract",
    "execution_contract_sha256",
})


@dataclass(frozen=True)
class JobStoreResult:
    job: ExecutionJob | None = None
    jobs: tuple[ExecutionJob, ...] = ()
    path: Path | None = None
    error: DomainError | None = None

    @property
    def accepted(self) -> bool:
        return self.error is None


class FileExecutionJobStore:
    def __init__(self, root: Path):
        self.root = root

    def create(self, job: ExecutionJob) -> JobStoreResult:
        with self._locked():
            active = self.find_active(job.project_id, job.task_id, job.transition_id)
            if not active.accepted:
                return JobStoreResult(error=active.error)
            if active.jobs:
                return JobStoreResult(error=DomainError(
                    code="job.active_exists",
                    message=(
                        "An active execution job already exists for "
                        f"task {job.task_id!r} and transition {job.transition_id!r}."
                    ),
                    location=active.jobs[0].job_id,
                ))

            now = utc_now()
            metadata = dict(job.metadata)
            metadata.setdefault("created_at", now)
            metadata.setdefault("updated_at", now)
            job_to_save = ExecutionJob(
                job_id=job.job_id,
                project_id=job.project_id,
                task_id=job.task_id,
                transition_id=job.transition_id,
                worker_id=job.worker_id,
                workspace_path=job.workspace_path,
                status=job.status,
                attempts=job.attempts,
                metadata=MappingProxyType(metadata),
            )
            return self.save(job_to_save)

    def save(self, job: ExecutionJob) -> JobStoreResult:
        path = self._path_for(job.job_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = _job_to_payload(job)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".job.",
                suffix=".tmp",
                dir=str(path.parent),
                text=True,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, sort_keys=True, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
                _fsync_directory(path.parent)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
        except (OSError, TypeError, ValueError) as exc:
            return JobStoreResult(error=DomainError(
                code="job.write_failed",
                message=f"Cannot write execution job: {exc}",
                location=str(path),
            ))
        return JobStoreResult(job=job, path=path)

    def get(self, job_id: str) -> JobStoreResult:
        path = self._path_for(job_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("job record must be an object")
            return JobStoreResult(job=_job_from_payload(payload), path=path)
        except FileNotFoundError:
            return JobStoreResult(error=DomainError(
                code="job.not_found",
                message=f"Execution job {job_id!r} was not found.",
                location=str(path),
            ))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return JobStoreResult(error=DomainError(
                code="job.read_failed",
                message=f"Cannot read execution job: {exc}",
                location=str(path),
            ))

    def update_status(
        self,
        job_id: str,
        status: ExecutionJobStatus | str,
        *,
        metadata: Mapping[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> JobStoreResult:
        loaded = self.get(job_id)
        if not loaded.accepted or loaded.job is None:
            return loaded
        job = loaded.job
        now = utc_now()
        merged_metadata = dict(job.metadata)
        updates = dict(metadata or {})
        for key in IMMUTABLE_JOB_METADATA_KEYS:
            if (
                key in updates
                and key in merged_metadata
                and updates[key] != merged_metadata[key]
            ):
                return JobStoreResult(error=DomainError(
                    code="job.immutable_metadata",
                    message=f"Execution job metadata field {key!r} is immutable.",
                    location=job_id,
                ))
        merged_metadata.update(updates)
        merged_metadata["updated_at"] = now
        updated = ExecutionJob(
            job_id=job.job_id,
            project_id=job.project_id,
            task_id=job.task_id,
            transition_id=job.transition_id,
            worker_id=job.worker_id,
            workspace_path=job.workspace_path,
            status=status,
            attempts=job.attempts + (1 if increment_attempts else 0),
            metadata=MappingProxyType(merged_metadata),
        )
        return self.save(updated)

    def list(self) -> JobStoreResult:
        jobs: list[ExecutionJob] = []
        if not self.root.exists():
            return JobStoreResult(jobs=())
        for path in sorted(self.root.glob("*/job.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError("job record must be an object")
                jobs.append(_job_from_payload(payload))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                return JobStoreResult(error=DomainError(
                    code="job.read_failed",
                    message=f"Cannot read execution job: {exc}",
                    location=str(path),
                ))
        return JobStoreResult(jobs=tuple(jobs))

    def find_active(self, project_id: str, task_id: str, transition_id: str) -> JobStoreResult:
        listed = self.list()
        if not listed.accepted:
            return listed
        jobs = tuple(
            job for job in listed.jobs
            if job.project_id == project_id
            and job.task_id == task_id
            and job.transition_id == transition_id
            and _status_value(job.status) in ACTIVE_JOB_STATUSES
        )
        return JobStoreResult(jobs=jobs)

    def _path_for(self, job_id: str) -> Path:
        return self.root / job_id / "job.json"

    def path_for(self, job_id: str) -> Path:
        return self._path_for(job_id)

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _job_to_payload(job: ExecutionJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "task_id": job.task_id,
        "transition_id": job.transition_id,
        "worker_id": job.worker_id,
        "workspace_path": job.workspace_path,
        "status": _status_value(job.status),
        "attempts": job.attempts,
        "metadata": dict(job.metadata),
    }


def _job_from_payload(payload: Mapping[str, Any]) -> ExecutionJob:
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("job metadata must be an object")
    return ExecutionJob(
        job_id=_required_string(payload, "job_id"),
        project_id=_required_string(payload, "project_id"),
        task_id=_required_string(payload, "task_id"),
        transition_id=_required_string(payload, "transition_id"),
        worker_id=_required_string(payload, "worker_id"),
        workspace_path=_required_string(payload, "workspace_path"),
        status=_required_string(payload, "status"),
        attempts=int(payload.get("attempts", 0)),
        metadata=MappingProxyType(dict(metadata)),
    )


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"job field {key!r} must be a non-empty string")
    return value


def _status_value(status: ExecutionJobStatus | str) -> str:
    return status.value if isinstance(status, ExecutionJobStatus) else str(status)


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
