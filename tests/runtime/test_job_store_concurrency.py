from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from open_tulid.domain import ExecutionJob
from open_tulid.runtime.jobs import FileExecutionJobStore


def test_attempt_updates_and_completion_preserve_each_others_evidence(tmp_path):
    root = tmp_path / "jobs"
    store = FileExecutionJobStore(root)
    assert store.create(ExecutionJob(job_id="job", project_id="project", task_id="task",
        transition_id="implement", worker_id="worker", workspace_path=str(tmp_path / "workspace"))).accepted
    barrier = Barrier(4)

    def writer(index):
        own_store = FileExecutionJobStore(root)
        barrier.wait()
        if index == 0:
            return own_store.update_status("job", "accepted", metadata={"acceptance_transaction_id": "committed"}).accepted
        for number in range(10):
            result = own_store.record_attempt("job", {
                "attempt_id": f"attempt-{index}-{number}", "job_id": "job",
                "attempt_number": index * 10 + number,
                "task_revision": "revision", "transition_id": "implement",
                "worker_id": "worker", "status": "ended",
            })
            assert result.accepted
        return True

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert all(workers.map(writer, range(4)))
    job = store.get("job").job
    assert str(getattr(job.status, "value", job.status)) == "accepted"
    assert job.metadata["acceptance_transaction_id"] == "committed"
    assert len(job.metadata["attempt_records"]) == 30


def test_status_updates_cannot_replace_frozen_planning_inputs(tmp_path):
    store = FileExecutionJobStore(tmp_path)
    assert store.create(ExecutionJob(job_id="job", project_id="project", task_id="task",
        transition_id="plan", worker_id="worker", workspace_path=str(tmp_path / "workspace"),
        metadata={"planning_inputs": {"sha256": "original"}})).accepted
    changed = store.update_status("job", "running", metadata={"planning_inputs": {"sha256": "replacement"}})
    assert not changed.accepted
    assert changed.error.code == "job.immutable_metadata"
    assert store.get("job").job.metadata["planning_inputs"]["sha256"] == "original"
