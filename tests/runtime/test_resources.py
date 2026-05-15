from pathlib import Path

from open_tulid.models import ResourceConfig
from open_tulid.runtime import FileResourceLeaseStore


def test_resource_lease_store_enforces_capacity_and_releases_by_job(tmp_path: Path):
    store = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )

    first = store.try_acquire(("local-llm",), job_id="job-1", worker_id="opencode")
    second = store.try_acquire(("local-llm",), job_id="job-2", worker_id="opencode")

    assert first.acquired is True
    assert second.acquired is False
    assert second.busy_resources == ("local-llm",)

    store.release_job("job-1")
    third = store.try_acquire(("local-llm",), job_id="job-2", worker_id="opencode")

    assert third.acquired is True


def test_resource_lease_store_recovers_orphan_reservation_with_missing_owner(tmp_path: Path):
    store = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )
    missing_job = tmp_path / "jobs" / "missing" / "job.json"
    assert store.try_acquire(
        ("local-llm",),
        job_id="missing",
        worker_id="opencode",
        owner_path=missing_job,
    ).acquired is True

    recovered = store.release_orphan_reservations()
    acquired = store.try_acquire(
        ("local-llm",),
        job_id="job-2",
        worker_id="opencode",
        owner_path=tmp_path / "jobs" / "job-2" / "job.json",
    )

    assert recovered == ("missing",)
    assert acquired.acquired is True
    assert [lease.job_id for lease in store.leases_for("local-llm")] == ["job-2"]


def test_try_acquire_does_not_reap_inflight_missing_owner(tmp_path: Path):
    store = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )
    assert store.try_acquire(
        ("local-llm",),
        job_id="job-1",
        worker_id="opencode",
        owner_path=tmp_path / "jobs" / "job-1" / "job.json",
    ).acquired is True

    second = store.try_acquire(
        ("local-llm",),
        job_id="job-2",
        worker_id="opencode",
        owner_path=tmp_path / "jobs" / "job-2" / "job.json",
    )

    assert second.acquired is False
    assert [lease.job_id for lease in store.leases_for("local-llm")] == ["job-1"]


def test_admit_persists_owner_while_holding_reservation(tmp_path: Path):
    store = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )
    owner = tmp_path / "jobs" / "job-1" / "job.json"

    def commit():
        owner.parent.mkdir(parents=True)
        owner.write_text("{}", encoding="utf-8")
        return "committed"

    reserved, committed = store.admit(
        ("local-llm",),
        job_id="job-1",
        worker_id="opencode",
        owner_path=owner,
        commit=commit,
    )

    assert reserved.acquired is True
    assert committed == "committed"


def test_admit_releases_reservation_when_commit_result_is_rejected(tmp_path: Path):
    store = FileResourceLeaseStore(
        tmp_path / "leases",
        {"local-llm": ResourceConfig(kind="model", capacity=1)},
    )

    reserved, committed = store.admit(
        ("local-llm",),
        job_id="job-1",
        worker_id="opencode",
        owner_path=tmp_path / "jobs" / "job-1" / "job.json",
        commit=lambda: False,
        accepted=lambda result: result is True,
    )

    assert reserved.acquired is True
    assert committed is False
    assert store.leases_for("local-llm") == ()
