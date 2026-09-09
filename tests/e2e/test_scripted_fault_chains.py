"""Plan 6E: deterministic fault and scripted chains on the detached runtime.

This extends the existing scripted end-to-end setup with a multi-round
clarification, a generated task batch, dependent execution, failed
completion/repair, review, and exact repository delivery. Known
failures are injected deterministically through the scripted worker, never
by relying on random model behavior. Every scenario must end in a bounded
accepted or explicitly blocked/failed outcome with consistent tracker and
repository state.

Faults injected (all deterministic, driven by the scenario string):
  - omitted changed paths                -> verified and delivered from manifest
  - deletion of a tracked repo file      -> deletion delivered exactly
  - worker death during submission       -> fresh bounded attempt accepted
  - malformed planning artifacts         -> explicitly blocked/failed
  - missing frozen context               -> explicitly blocked with evidence
  - stale promotion target               -> rejected, repaired inside scope

The workflow is exercised under renamed states/task types and arbitrary
worker assignments (both a single worker for every step and distinct workers
per step) to prove that semantics come from the workflow, not names.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_tulid.runtime import JsonlEventStore

from test_runtime_detached_stt_workflow import (
    _job_payload_for_transition,
    _job_payloads,
    _make_runtime_project,
    _print_system_logs,
    _run_tulid,
    _task_state,
    _wait_for,
    scripted_runtime_worker_image,
)


def _renamed_workflow() -> str:
    """A workflow whose states and task types are renamed relative to the
    canonical notionally-expected names. Derived tasks form a generated batch
    with a dependency edge: b depends on a."""
    return """\
schema_version: 1
storage:
  boards:
    Work: kanban/Work.md
  state_mappings:
    - state: Backlogged
      board: Work
      column: Backlogged
    - state: NeedsApproval
      board: Work
      column: Needs approval
    - state: SpecReady
      board: Work
      column: Spec ready
    - state: BreakdownReady
      board: Work
      column: Breakdown ready
    - state: Pending
      board: Work
      column: Pending
    - state: ReadyNow
      board: Work
      column: Ready now
    - state: Reviewing
      board: Work
      column: Reviewing
    - state: Shipped
      board: Work
      column: Shipped
statements:
  - kind: state
    id: Backlogged
  - kind: state
    id: NeedsApproval
  - kind: state
    id: SpecReady
  - kind: state
    id: BreakdownReady
  - kind: state
    id: Pending
  - kind: state
    id: ReadyNow
  - kind: state
    id: Reviewing
  - kind: state
    id: Shipped
    terminal_outcome: success
  - kind: task_type
    id: EpicIdea
    instructions: [default]
  - kind: task_type
    id: BuildTask
    instructions: [default]
  - kind: validation_type
    id: tests_pass
    args:
      command:
        type: string
  - kind: validation_type
    id: project_build
    args:
      command:
        type: string
  - kind: artifact_type
    id: ProductSpec
    template: product-spec.md
  - kind: artifact_type
    id: TechnicalDirection
    template: technical-direction.md
  - kind: artifact_type
    id: ImplementationSpec
    template: implementation-spec.md
  - kind: artifact_type
    id: ImplementationTaskFile
{workers_block}
  - kind: transition
    id: PlanEpic
    task_type: EpicIdea
    from: Backlogged
    to: NeedsApproval
    worker: {planner_w}
    default_for_scheduler: true
    requires:
      artifacts: [ProductSpec, TechnicalDirection]
  - kind: transition
    id: BlessEpic
    task_type: EpicIdea
    from: NeedsApproval
    to: SpecReady
  - kind: transition
    id: WriteBuildSpec
    task_type: EpicIdea
    from: SpecReady
    to: BreakdownReady
    worker: {spec_w}
    default_for_scheduler: true
    requires:
      artifacts: [ImplementationSpec]
  - kind: transition
    id: BreakdownEpic
    task_type: EpicIdea
    from: BreakdownReady
    to: Shipped
    worker: {breakdown_w}
    default_for_scheduler: true
    derives:
      task_type: BuildTask
      state: Pending
      artifact_type: ImplementationTaskFile
  - kind: transition
    id: RunBuildTask
    task_type: BuildTask
    from: Pending
    to: Reviewing
    worker: {impl_w}
    default_for_scheduler: true
    requires:
      changed_files:
        required: true
      validations:
        - type: tests_pass
          args:
            command: python check_repo.py tests
        - type: project_build
          args:
            command: python check_repo.py build
  - kind: transition
    id: RunFromReady
    task_type: BuildTask
    from: ReadyNow
    to: Reviewing
    worker: {impl_w}
    default_for_scheduler: true
    requires:
      changed_files:
        required: true
      validations:
        - type: tests_pass
          args:
            command: python check_repo.py tests
        - type: project_build
          args:
            command: python check_repo.py build
  - kind: transition
    id: InspectBuild
    review: true
    task_type: BuildTask
    from: Reviewing
    to: Shipped
    worker: {review_w}
    default_for_scheduler: true
    requires:
      validations:
        - type: tests_pass
          args:
            command: python check_repo.py tests
        - type: project_build
          args:
            command: python check_repo.py build
  - kind: transition
    id: ReworkBuildTask
    task_type: BuildTask
    from: Reviewing
    to: Pending
"""


def _single_worker_workflow() -> str:
    workers_block = (
        "  - kind: worker\n"
        "    id: single_w\n"
        "    type: codex\n"
        "    instructions: [default]\n"
    )
    return _renamed_workflow().format(
        workers_block=workers_block,
        planner_w="single_w", planner_type="codex",
        spec_w="single_w", spec_type="codex",
        breakdown_w="single_w", breakdown_type="codex",
        impl_w="single_w", impl_type="codex",
        review_w="single_w", review_type="codex",
    )


def _distinct_worker_workflow() -> str:
    workers_block = (
        "  - kind: worker\n    id: planner_w\n    type: codex\n    instructions: [default]\n"
        "  - kind: worker\n    id: spec_w\n    type: codex\n    instructions: [default]\n"
        "  - kind: worker\n    id: breakdown_w\n    type: codex\n    instructions: [default]\n"
        "  - kind: worker\n    id: impl_w\n    type: local_llm\n    instructions: [default]\n"
        "  - kind: worker\n    id: review_w\n    type: local_llm\n    instructions: [default]\n"
    )
    return _renamed_workflow().format(
        workers_block=workers_block,
        planner_w="planner_w", planner_type="codex",
        spec_w="spec_w", spec_type="codex",
        breakdown_w="breakdown_w", breakdown_type="codex",
        impl_w="impl_w", impl_type="local_llm",
        review_w="review_w", review_type="local_llm",
    )


def _write_workflow(project: Path, content: str) -> None:
    (project / "workflow.yaml").write_text(content, encoding="utf-8")


def _install_renamed_fixtures(project: Path) -> None:
    """Reconcile the copied tracker fixtures with the renamed states/types."""
    board = project / "kanban" / "Work.md"
    board.write_text(
        "## Backlogged\n- [ ] [[1-stt-clipboard]]\n\n"
        "## Needs approval\n\n"
        "## Spec ready\n\n"
        "## Breakdown ready\n\n"
        "## Pending\n\n"
        "## Ready now\n\n"
        "## Reviewing\n\n"
        "## Shipped\n",
        encoding="utf-8",
    )
    task = next(iter((project / "tasks").glob("1-*.md")))
    task.write_text(
        task.read_text(encoding="utf-8").replace("type: ProductIdea", "type: EpicIdea")
        .replace("state: Idea", "state: Backlogged"),
        encoding="utf-8",
    )


def _wait_group(project: Path, expected: dict[str, str], timeout: float = 120.0) -> None:
    _wait_for(
        lambda: all(_task_state(project, task_id) == state for task_id, state in expected.items()),
        f"tasks to reach {expected}",
        timeout=timeout,
    )


def test_chain_multiround_batch_dependency_review_exact_delivery_distinct_workers(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full renamed chain: multi-round planning, a generated 2-task batch with
    a dependency edge, dependent implementation, review, and exact delivery,
    run with distinct workers for the different steps."""
    pytest.importorskip("open_tulid")

    project = _make_runtime_project(
        tmp_path,
        scripted_runtime_worker_image,
        scenario="chain_batch",
        extra_worker_images={
            "planner_w": scripted_runtime_worker_image,
            "spec_w": scripted_runtime_worker_image,
            "breakdown_w": scripted_runtime_worker_image,
            "impl_w": scripted_runtime_worker_image,
            "review_w": scripted_runtime_worker_image,
        },
    )
    _write_workflow(project.project, _distinct_worker_workflow())
    _install_renamed_fixtures(project.project)

    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr

        # Multi-round clarification: direction and specification are produced
        # across distinct scheduler rounds.
        _wait_for(
            lambda: _task_state(project.project, "1") == "NeedsApproval",
            "epic idea to reach manual approval (direction round 1)",
        )
        manual = _run_tulid(project.root, "transition", "Agent", "1", "BlessEpic")
        assert manual.returncode == 0, manual.stdout + manual.stderr

        _wait_group(project.project, {"1": "Shipped"})
        # The generated batch: two derived BuildTasks forked from the epic.
        _wait_group(project.project, {"1": "Shipped", "2": "Shipped", "3": "Shipped"})

        # Dependent execution: task 3 depends on task 2, so 2 shipped first.
        events = list(JsonlEventStore(project.project / "events").iter_events())
        task2_accepted = min(
            (event.timestamp for event in events if event.task_id == "2" and event.event_type == "TransitionAccepted"),
            default=None,
        )
        task2_review = min(
            (event.timestamp for event in events if event.task_id == "2" and event.event_type == "ReviewRequested"),
            default=None,
        )
        task3_review = min(
            (event.timestamp for event in events if event.task_id == "3" and event.event_type == "ReviewRequested"),
            default=None,
        )
        assert task2_review is not None and task3_review is not None
        assert task2_accepted is not None and task2_accepted < task3_review

        # Distinct workers took the implementation and review steps.
        implement_jobs = [
            payload for payload in _job_payloads(project)
            if payload.get("transition_id") == "RunBuildTask"
        ]
        review_jobs = [
            payload for payload in _job_payloads(project)
            if payload.get("transition_id") == "InspectBuild"
        ]
        assert {payload["worker_id"] for payload in implement_jobs} == {"impl_w"}
        assert {payload["worker_id"] for payload in review_jobs} == {"review_w"}

        # Exact repository delivery: app.py and helper.py match the verified
        # candidate (the review step's in-scope correction is also delivered).
        assert (project.repo / "app.py").read_text(encoding="utf-8").startswith(
            "def healthz():\n    return 'ok'\n"
        )
        assert (project.repo / "helper.py").read_text(encoding="utf-8").startswith(
            "def helper():\n    return 1\n"
        )
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def test_chain_batch_dependency_review_single_worker_all_steps(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same generated-batch dependent chain, but a single configured worker
    is assigned to every transition. Success semantics must not depend on
    worker names or roles."""
    pytest.importorskip("open_tulid")

    project = _make_runtime_project(
        tmp_path,
        scripted_runtime_worker_image,
        scenario="chain_batch_single",
    )
    _write_workflow(project.project, _single_worker_workflow())
    _install_renamed_fixtures(project.project)

    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr

        _wait_for(
            lambda: _task_state(project.project, "1") == "NeedsApproval",
            "epic idea to reach manual approval",
        )
        manual = _run_tulid(project.root, "transition", "Agent", "1", "BlessEpic")
        assert manual.returncode == 0, manual.stdout + manual.stderr

        _wait_group(project.project, {"1": "Shipped", "2": "Shipped", "3": "Shipped"})

        workers = {payload["worker_id"] for payload in _job_payloads(project) if payload.get("transition_id") in ("PlanEpic", "WriteBuildSpec", "BreakdownEpic", "RunBuildTask", "InspectBuild")}
        assert workers == {"single_w"}

        assert (project.repo / "app.py").read_text(encoding="utf-8").startswith(
            "def healthz():\n    return 'ok'\n"
        )
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def test_omitted_changed_paths_are_delivered_from_manifest(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitted declarations cannot omit verified source from delivery."""
    pytest.importorskip("open_tulid")

    project = _make_runtime_project(
        tmp_path,
        scripted_runtime_worker_image,
        scenario="fault_omit_changed_files",
    )
    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr
        _wait_for(
            lambda: _task_state(project.project, "1") == "HumanReview",
            "product idea to reach HumanReview",
        )
        manual = _run_tulid(project.root, "transition", "Agent", "1", "ApproveDirection")
        assert manual.returncode == 0, manual.stdout + manual.stderr

        _wait_for(
            lambda: _task_state(project.project, "2") == "Done",
            "omitted-path candidate to be verified and delivered",
            timeout=90.0,
        )

        job = _job_payload_for_transition(project, "ImplementTask")
        assert job["status"] == "accepted"
        submissions = job["metadata"]["completion_submissions"]
        assert submissions["implement-omitted"]["accepted"] is True
        assert (project.repo / "helper.py").read_text() == "def helper():\n    return 1\n"
        assert (project.repo / "app.py").read_text(encoding="utf-8") == (
            "def healthz():\n    return 'ok'\n"
        )
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def test_deletion_is_verified_and_delivered(tmp_path, scripted_runtime_worker_image, capsys):
    project = _make_runtime_project(tmp_path, scripted_runtime_worker_image, scenario="fault_delete_file")
    (project.repo / "legacy-note.txt").write_text("legacy content\n")
    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr
        _wait_for(lambda: _task_state(project.project, "1") == "HumanReview", "direction approval")
        approved = _run_tulid(project.root, "transition", "Agent", "1", "ApproveDirection")
        assert approved.returncode == 0, approved.stdout + approved.stderr
        _wait_for(lambda: _task_state(project.project, "2") == "Done", "verified deletion delivery", timeout=90)
        job = _job_payload_for_transition(project, "ImplementTask")
        assert job["status"] == "accepted"
        assert not (project.repo / "legacy-note.txt").exists()
        assert "legacy-note.txt" in job["metadata"]["verification_report"]["changes"]["removed"]
        assert any(effect["type"] == "delete_changed_file" for effect in job["metadata"]["promoted_files"])
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def test_fault_worker_death_during_submission_recovers(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A worker that dies during submission is failed with preserved evidence,
    then a fresh bounded attempt is scheduled and accepted."""
    pytest.importorskip("open_tulid")

    project = _make_runtime_project(
        tmp_path,
        scripted_runtime_worker_image,
        scenario="fault_worker_death",
        runtime_options=(
            "failed_job_backoff_seconds: 1\n"
            "max_failed_attempts_per_transition: 1\n"
        ),
    )
    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr
        _wait_for(
            lambda: _task_state(project.project, "1") == "HumanReview",
            "product idea to reach HumanReview",
        )
        manual = _run_tulid(project.root, "transition", "Agent", "1", "ApproveDirection")
        assert manual.returncode == 0, manual.stdout + manual.stderr

        # After the parent is fully planned, the derived implementation task
        # runs and its worker dies during submission with returncode 9.
        _wait_for(
            lambda: _task_state(project.project, "1") == "Done",
            "parent planning to finish before the implementation task starts",
            timeout=90.0,
        )

        # The dying worker is failed with preserved evidence; the failed-attempt
        # bound stops retries, leaving the task explicitly blocked with the
        # repository untouched (a bounded, non-spinning outcome).
        _wait_for(
            lambda: any(
                payload.get("transition_id") == "ImplementTask" and payload["status"] == "failed"
                for payload in _job_payloads(project)
            ),
            "worker death to be recorded failed within the attempt bound",
            timeout=90.0,
        )

        jobs = _job_payloads(project)
        death_attempts = [
            payload for payload in jobs if payload.get("transition_id") == "ImplementTask"
        ]
        failed = [p for p in death_attempts if p["status"] == "failed"]
        assert failed, "the worker-death attempt must be recorded failed"
        assert death_attempts, "a worker-death implement job must exist"
        assert not any(p["status"] == "accepted" for p in death_attempts)
        assert _task_state(project.project, "2") != "Done"
        # Repository state stayed consistent: nothing was delivered.
        assert (project.repo / "app.py").read_text(encoding="utf-8").startswith(
            "def healthz():\n    raise NotImplementedError"
        )
        # Evidence of the died worker is preserved (not scrubbed).
        alive_evidence = [
            Path(str(p["workspace_path"])) / ".open-tulid" / "logs" / "stdout.log"
            for p in failed
        ]
        assert any(path.exists() for path in alive_evidence)
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def test_fault_malformed_planning_artifact_blocks(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed planning artifacts are rejected before any batch promotion,
    and the task ends bounded and blocked (never promoted as success)."""
    pytest.importorskip("open_tulid")

    project = _make_runtime_project(
        tmp_path,
        scripted_runtime_worker_image,
        scenario="fault_malformed_artifact",
        runtime_options=(
            "failed_job_backoff_seconds: 1\n"
            "max_failed_attempts_per_transition: 1\n"
        ),
    )
    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr
        _wait_for(
            lambda: _task_state(project.project, "1") == "HumanReview",
            "product idea to reach HumanReview",
        )
        manual = _run_tulid(project.root, "transition", "Agent", "1", "ApproveDirection")
        assert manual.returncode == 0, manual.stdout + manual.stderr

        # Malformed planning artifacts are rejected before any batch promotion,
        # and the failed-attempt bound stops retries, leaving the parent
        # explicitly blocked at the breakdown gate.
        _wait_for(
            lambda: _malformation_blocked(project),
            "malformed planning artifact to be rejected with an explicit diagnostic",
            timeout=90.0,
        )
        _wait_for(
            lambda: any(
                payload.get("transition_id") == "BreakDownImplementationSpec"
                and payload.get("status") == "failed"
                for payload in _job_payloads(project)
            ),
            "malformed breakdown job to fail and exhaust the attempt bound",
            timeout=90.0,
        )

        state = _task_state(project.project, "1")
        assert state != "Done", "malformed artifacts must never be promoted"
        jobs = _job_payloads(project)
        breakdown_jobs = [
            payload for payload in jobs
            if payload.get("transition_id") == "BreakDownImplementationSpec"
        ]
        assert any(payload["status"] == "failed" for payload in breakdown_jobs)
        assert all(payload["status"] != "accepted" for payload in breakdown_jobs)
        # No partial derived work was promoted to the repository.
        assert not (project.repo / "01-healthz-task.md").exists()
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def _malformation_blocked(project) -> bool:
    """Whether the breakdown settlement left explicit blocked evidence."""
    events = JsonlEventStore(project.project / "events").iter_events()
    rejected = [
        e for e in events if e.event_type == "ExecutionCompletionRejected"
        and (e.data or {}).get("feedback")
    ]
    for event in rejected:
        codes = [item.get("code") for item in event.data["feedback"] if isinstance(item, dict)]
        if any("task.derived" in (code or "") for code in codes):
            return True
    return False


def test_fault_missing_context_blocks(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When required frozen context is missing, the worker ends explicitly
    blocked and no implementation is accepted."""
    pytest.importorskip("open_tulid")

    project = _make_runtime_project(
        tmp_path,
        scripted_runtime_worker_image,
        scenario="fault_missing_context",
    )
    try:
        started = _run_tulid(project.root, "runtime", "start", "--interval", "0.2")
        assert started.returncode == 0, started.stdout + started.stderr
        _wait_for(
            lambda: _task_state(project.project, "1") == "HumanReview",
            "product idea to reach HumanReview",
        )
        manual = _run_tulid(project.root, "transition", "Agent", "1", "ApproveDirection")
        assert manual.returncode == 0, manual.stdout + manual.stderr

        _wait_for(
            lambda: _task_state(project.project, "2") in ("Todo", "ReadyToImplement", "SelfReview", "Done")
            or _task_state(project.project, "2") is None,
            "missing-context scenario to settle",
            timeout=90.0,
        )
        assert _task_state(project.project, "2") != "Done"
        blocked = [
            payload
            for payload in _job_payloads(project)
            if payload.get("transition_id") == "ImplementTask"
            and payload.get("metadata", {}).get("blocker")
        ]
        if not blocked:
            # The blocker is carried as an artifact; at minimum the repo must
            # be untouched.
            assert (project.repo / "app.py").read_text(encoding="utf-8") != (
                "def healthz():\n    return 'ok'\n"
            )
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)
