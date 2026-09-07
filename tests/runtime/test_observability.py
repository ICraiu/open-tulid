from __future__ import annotations

import threading
import time
from typing import Callable, List

from open_tulid.runtime.observability import (
    WorkerExited,
    WorkerObservability,
)


JOB_ID = "job-1"
ATTEMPT_ID = "7"


class _Probe:
    def __init__(self) -> None:
        self._alive = True

    def set_alive(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _StatusReader:
    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}

    def set_status(self, job_id: str, status: str) -> None:
        self.statuses[job_id] = status

    def read(self, job_id: str) -> str:
        return self.statuses.get(job_id, "running")


def _register(
    observability: WorkerObservability,
    *,
    probe: _Probe | None = None,
    status_reader: _StatusReader | None = None,
    on_exited: Callable[[WorkerExited], None] | None = None,
    job_id: str = JOB_ID,
    attempt_id: str = ATTEMPT_ID,
    check_interval_seconds: float | None = None,
) -> tuple[_Probe, _StatusReader, list[WorkerExited]]:
    probe = probe or _Probe()
    status_reader = status_reader or _StatusReader()
    exits: list[WorkerExited] = []
    observability.register(
        job_id=job_id,
        attempt_id=attempt_id,
        probe=probe,
        read_status=status_reader.read,
        on_exited=on_exited or (lambda event: exits.append(event)),
        task_id="task-1",
    )
    return probe, status_reader, exits


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def test_liveness_is_checked_at_configured_interval(tmp_path):
    observable = WorkerObservability(check_interval_seconds=0.01)
    probe = _Probe()
    checked: list[float] = []
    original_alive = probe.is_alive

    def counting_alive():
        checked.append(time.monotonic())
        return original_alive()

    class CountingProbe:
        def is_alive(self):
            counting_alive()
            return True

    observable.register(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        probe=CountingProbe(),
        read_status=lambda job_id: "running",
        on_exited=lambda event: None,
    )
    try:
        time.sleep(0.15)
        # The probe must have been sampled multiple times, not just a single
        # stale jittered check.
        assert len(checked) >= 3
        deltas = [checked[i + 1] - checked[i] for i in range(len(checked) - 1)]
        assert all(delta <= 0.2 for delta in deltas)
    finally:
        observable.close()


def test_unexpected_worker_exit_emits_worker_exited():
    observable = WorkerObservability(check_interval_seconds=0.001)
    probe, _reader, exits = _register(observable)
    probe.set_alive(False)
    assert _wait_until(lambda: len(exits) > 0)
    assert exits[0].job_id == JOB_ID
    assert exits[0].attempt_id == ATTEMPT_ID
    assert exits[0].returncode is None
    observable.close()


def test_unexpected_worker_exit_includes_returncode_when_known():
    observable = WorkerObservability(check_interval_seconds=0.001)
    probe = _Probe()
    reader = _StatusReader()
    exits: list[WorkerExited] = []
    observable.register(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        probe=probe,
        read_status=reader.read,
        on_exited=lambda event: exits.append(event),
        task_id="task-1",
        returncode=17,
    )
    probe.set_alive(False)
    assert _wait_until(lambda: len(exits) > 0)
    assert exits[0].returncode == 17
    observable.close()


def test_accepted_completion_followed_by_normal_exit_is_not_faulty():
    observable = WorkerObservability(check_interval_seconds=0.001)
    probe = _Probe()
    reader = _StatusReader()
    reader.set_status(JOB_ID, "accepted")
    exits: list[WorkerExited] = []
    observable.register(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        probe=probe,
        read_status=reader.read,
        on_exited=lambda event: exits.append(event),
        task_id="task-1",
    )
    probe.set_alive(False)
    time.sleep(0.05)
    assert exits == []
    observable.close()


def test_terminal_status_followed_by_normal_exit_is_not_faulty():
    observable = WorkerObservability(check_interval_seconds=0.001)
    probe = _Probe()
    reader = _StatusReader()
    reader.set_status(JOB_ID, "failed")
    exits: list[WorkerExited] = []
    observable.register(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        probe=probe,
        read_status=reader.read,
        on_exited=lambda event: exits.append(event),
        task_id="task-1",
    )
    probe.set_alive(False)
    time.sleep(0.05)
    assert exits == []
    observable.close()


def test_completion_under_validation_is_kept_alive_until_it_accepts():
    observable = WorkerObservability(check_interval_seconds=0.001)
    probe = _Probe()
    reader = _StatusReader()
    reader.set_status(JOB_ID, "completion_submitted")
    exits: list[WorkerExited] = []

    def on_exited(event: WorkerExited) -> None:
        exits.append(event)

    observable.register(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        probe=probe,
        read_status=reader.read,
        on_exited=on_exited,
        task_id="task-1",
    )
    probe.set_alive(False)
    # The worker is gone but validation is ongoing; not yet a fault.
    time.sleep(0.05)
    assert exits == []
    # Validation finishes and accepts; the worker must remain non-faulty.
    reader.set_status(JOB_ID, "accepted")
    time.sleep(0.05)
    assert exits == []
    observable.close()


def test_duplicate_exit_notifications_are_harmless():
    observable = WorkerObservability(check_interval_seconds=0.001)
    probe = _Probe()
    reader = _StatusReader()
    exits: list[WorkerExited] = []
    observable.register(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        probe=probe,
        read_status=reader.read,
        on_exited=lambda event: exits.append(event),
        task_id="task-1",
    )
    probe.set_alive(False)
    time.sleep(0.1)
    assert len(exits) == 1
    observable.close()


def test_unregister_stops_monitoring_and_does_not_emit():
    observable = WorkerObservability(check_interval_seconds=0.001)
    probe, _reader, exits = _register(observable)
    observable.unregister(job_id=JOB_ID)
    probe.set_alive(False)
    time.sleep(0.05)
    assert exits == []
    observable.close()


def test_stale_job_identity_cannot_affect_current_attempt():
    observable = WorkerObservability(check_interval_seconds=0.001)
    # A second, current attempt is supervised; a stale attempt key is ignored.
    observer2_probe = _Probe()
    reader2 = _StatusReader()
    current_exits: list[WorkerExited] = []
    stale_exits: list[WorkerExited] = []

    observable.register(
        job_id="job-other",
        attempt_id="1",
        probe=observer2_probe,
        read_status=reader2.read,
        on_exited=lambda event: stale_exits.append(event),
        task_id="task-x",
    )
    # Create the current attempt with a different job id.
    current_probe = _Probe()
    current_reader = _StatusReader()
    observable.register(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        probe=current_probe,
        read_status=current_reader.read,
        on_exited=lambda event: current_exits.append(event),
        task_id="task-1",
    )
    # The stale worker dies; only its own observer should fire.
    observer2_probe.set_alive(False)
    assert _wait_until(lambda: len(stale_exits) > 0)
    current_probe.set_alive(False)
    assert _wait_until(lambda: len(current_exits) > 0)
    assert current_exits[0].job_id == JOB_ID
    assert [e.job_id for e in stale_exits] == ["job-other"]
    observable.close()
