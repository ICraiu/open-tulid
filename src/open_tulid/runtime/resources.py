from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from pathlib import Path
from typing import Callable, TypeVar

from open_tulid.models import ResourceConfig

from .events import utc_now


@dataclass(frozen=True)
class ResourceLease:
    resource_id: str
    job_id: str
    worker_id: str
    acquired_at: str
    owner_path: str | None = None


@dataclass(frozen=True)
class ResourceLeaseResult:
    acquired: bool
    leases: tuple[ResourceLease, ...] = ()
    busy_resources: tuple[str, ...] = ()


T = TypeVar("T")


class FileResourceLeaseStore:
    def __init__(self, root: Path, resources: dict[str, ResourceConfig]):
        self.root = root
        self.resources = resources

    def available(self, resource_ids: tuple[str, ...]) -> tuple[bool, tuple[str, ...]]:
        with self._locked():
            busy = tuple(resource_id for resource_id in resource_ids if not self._has_capacity(resource_id))
        return not busy, busy

    def try_acquire(
        self,
        resource_ids: tuple[str, ...],
        *,
        job_id: str,
        worker_id: str,
        owner_path: Path | None = None,
    ) -> ResourceLeaseResult:
        with self._locked():
            busy = tuple(resource_id for resource_id in resource_ids if not self._has_capacity(resource_id))
            if busy:
                return ResourceLeaseResult(acquired=False, busy_resources=busy)
            leases = tuple(
                ResourceLease(
                    resource_id=resource_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    acquired_at=utc_now(),
                    owner_path=str(owner_path) if owner_path is not None else None,
                )
                for resource_id in resource_ids
            )
            for lease in leases:
                self._write_lease(lease)
        return ResourceLeaseResult(acquired=True, leases=leases)

    def admit(
        self,
        resource_ids: tuple[str, ...],
        *,
        job_id: str,
        worker_id: str,
        owner_path: Path,
        commit: Callable[[], T],
        accepted: Callable[[T], bool] | None = None,
    ) -> tuple[ResourceLeaseResult, T | None]:
        with self._locked():
            busy = tuple(resource_id for resource_id in resource_ids if not self._has_capacity(resource_id))
            if busy:
                return ResourceLeaseResult(acquired=False, busy_resources=busy), None
            leases = tuple(
                ResourceLease(
                    resource_id=resource_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    acquired_at=utc_now(),
                    owner_path=str(owner_path),
                )
                for resource_id in resource_ids
            )
            for lease in leases:
                self._write_lease(lease)
            try:
                committed = commit()
            except Exception:
                self._release_job_unlocked(job_id)
                raise
            if accepted is not None and not accepted(committed):
                self._release_job_unlocked(job_id)
            return ResourceLeaseResult(acquired=True, leases=leases), committed

    def job_holds(self, resource_ids: tuple[str, ...], job_id: str) -> bool:
        with self._locked():
            return all(
                any(lease.job_id == job_id for lease in self.leases_for(resource_id))
                for resource_id in resource_ids
            )

    def release_job(self, job_id: str) -> None:
        with self._locked():
            self._release_job_unlocked(job_id)

    def leases_for(self, resource_id: str) -> tuple[ResourceLease, ...]:
        leases: list[ResourceLease] = []
        lease_dir = self.root / resource_id / "leases"
        if not lease_dir.exists():
            return ()
        for path in sorted(lease_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                leases.append(ResourceLease(
                    resource_id=resource_id,
                    job_id=str(payload["job_id"]),
                    worker_id=str(payload["worker_id"]),
                    acquired_at=str(payload["acquired_at"]),
                    owner_path=str(payload["owner_path"]) if payload.get("owner_path") is not None else None,
                ))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return tuple(leases)

    def _has_capacity(self, resource_id: str) -> bool:
        resource = self.resources.get(resource_id)
        if resource is None:
            return False
        return len(self.leases_for(resource_id)) < resource.capacity

    def _write_lease(self, lease: ResourceLease) -> None:
        path = self.root / lease.resource_id / "leases" / f"{lease.job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "resource_id": lease.resource_id,
            "job_id": lease.job_id,
            "worker_id": lease.worker_id,
            "acquired_at": lease.acquired_at,
            "owner_path": lease.owner_path,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".lease.", suffix=".tmp", dir=str(path.parent), text=True)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _release_job_unlocked(self, job_id: str) -> None:
        if not self.root.exists():
            return
        for path in self.root.glob("*/leases/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("job_id") == job_id:
                path.unlink(missing_ok=True)

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def release_orphan_reservations(self) -> tuple[str, ...]:
        removed: list[str] = []
        with self._locked():
            removed.extend(self._release_orphan_reservations_unlocked())
        return tuple(removed)

    def release_inactive_reservations(self, active_job_ids: set[str]) -> tuple[str, ...]:
        """Release leases for jobs the coordinator no longer considers active."""
        removed: list[str] = []
        with self._locked():
            if not self.root.exists():
                return ()
            for path in self.root.glob("*/leases/*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                job_id = payload.get("job_id")
                if isinstance(job_id, str) and job_id not in active_job_ids:
                    path.unlink(missing_ok=True)
                    removed.append(job_id)
        return tuple(removed)

    def _release_orphan_reservations_unlocked(self) -> list[str]:
        removed: list[str] = []
        if not self.root.exists():
            return removed
        for path in self.root.glob("*/leases/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            owner_path = payload.get("owner_path")
            if isinstance(owner_path, str) and owner_path and not Path(owner_path).exists():
                path.unlink(missing_ok=True)
                removed.append(str(payload.get("job_id", "")))
        return removed
