from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


TERMINAL_EXECUTOR_STATUSES = frozenset({"accepted", "failed", "stale", "cancelled"})
COMPLETION_UNDER_VALIDATION_STATUSES = frozenset({"completion_submitted"})


@dataclass(frozen=True)
class WorkerExited:
    """Liveness supervisor result: a worker/container vanished unexpectedly.

    It carries the job_id plus attempt identity so the executor can reject a
    stale process, workspace, or old job event for the current attempt.
    """

    job_id: str
    attempt_id: str
    returncode: int | None = None


class WorkerLivenessProbe(Protocol):
    """Answers the only supervision question: is the worker still running?

    It must reflect actual process/container liveness, never GPU usage, never a
    stored job status, and never parsed agent log text.
    """

    def is_alive(self) -> bool: ...


class _ObservedWorker:
    __slots__ = (
        "job_id",
        "attempt_id",
        "task_id",
        "probe",
        "read_status",
        "on_exited",
        "check_interval_seconds",
        "_stop",
        "_thread",
        "_handled",
        "_lock",
        "_returncode",
    )

    def __init__(
        self,
        *,
        job_id: str,
        attempt_id: str,
        task_id: str | None,
        probe: WorkerLivenessProbe,
        read_status: Callable[[str], str],
        on_exited: Callable[[WorkerExited], None],
        check_interval_seconds: float,
        returncode: int | None = None,
    ) -> None:
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.task_id = task_id
        self.probe = probe
        self.read_status = read_status
        self.on_exited = on_exited
        self.check_interval_seconds = max(0.001, check_interval_seconds)
        self._returncode = returncode
        self._stop = threading.Event()
        self._handled = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"open-tulid-observe-{self.job_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._check_once():
                    return
                if self._stop.wait(self.check_interval_seconds):
                    return
        finally:
            with self._lock:
                self._handled = True

    def _check_once(self) -> bool:
        with self._lock:
            if self._handled:
                return True
            alive = self.probe.is_alive()
            if alive:
                return False
            status = self.read_status(self.job_id)
            # An accepted/terminal outcome means the worker completed normally;
            # a vanished process after such an outcome is never a fault.
            if status in TERMINAL_EXECUTOR_STATUSES:
                self._handled = True
                return True
            # A completion is being validated. Keep the worker/job alive until
            # validation finishes; do not fail it here.
            if status in COMPLETION_UNDER_VALIDATION_STATUSES:
                return False
            self._handled = True
            self.on_exited(WorkerExited(
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                returncode=self._returncode,
            ))
            return True

    def is_thread_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()


@dataclass
class WorkerObservability:
    """Executor-owned supervisor over active worker processes/containers.

    It periodically probes liveness at a bounded interval (configurable for
    tests, about once a minute outside tests) and emits an internal
    WorkerExited event to its owner when a worker disappears unexpectedly.
    """

    check_interval_seconds: float = 60.0
    _workers: dict[tuple[str, str], _ObservedWorker] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(
        self,
        *,
        job_id: str,
        attempt_id: str,
        probe: WorkerLivenessProbe,
        read_status: Callable[[str], str],
        on_exited: Callable[[WorkerExited], None],
        task_id: str | None = None,
        returncode: int | None = None,
    ) -> None:
        with self._lock:
            worker = _ObservedWorker(
                job_id=job_id,
                attempt_id=attempt_id,
                task_id=task_id,
                probe=probe,
                read_status=read_status,
                on_exited=on_exited,
                check_interval_seconds=self.check_interval_seconds,
                returncode=returncode,
            )
            previous = self._workers.get((job_id, attempt_id))
            if previous is not None:
                previous.stop()
            self._workers[(job_id, attempt_id)] = worker
            worker.start()

    def unregister(self, *, job_id: str) -> None:
        with self._lock:
            keys = [key for key in self._workers if key[0] == job_id]
            for key in keys:
                worker = self._workers.pop(key, None)
                if worker is not None:
                    worker.stop()

    def has_worker(self, *, job_id: str, attempt_id: str) -> bool:
        with self._lock:
            return (job_id, attempt_id) in self._workers

    def close(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()


__all__ = [
    "COMPLETION_UNDER_VALIDATION_STATUSES",
    "TERMINAL_EXECUTOR_STATUSES",
    "WorkerExited",
    "WorkerLivenessProbe",
    "WorkerObservability",
]
