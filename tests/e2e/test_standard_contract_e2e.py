from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from open_tulid.runtime import JsonlEventStore

from test_runtime_detached_stt_workflow import (
    _docker_available,
    _job_payload_for_transition,
    _job_payloads,
    _job_stdout,
    _make_runtime_project,
    _print_system_logs,
    _run_tulid,
    _task_state,
    _wait_for,
    scripted_runtime_worker_image,
)

STANDARD_CONTRACT = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: build
    argv: [python, check_repo.py, build]
    working_directory: .
    timeout_seconds: 120
    expect:
      exit_code: 0
  - name: tests
    argv: [python, check_repo.py, tests]
    working_directory: .
    timeout_seconds: 120
    expect:
      exit_code: 0
retry:
  max_attempts: 3
  visible_feedback: true
"""


def test_standard_contract_drives_minimal_delta_with_baseline_and_bounded_retry(
    tmp_path: Path,
    scripted_runtime_worker_image: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real runtime run proves the project global contract end to end.

    - the project global contract.yaml is loaded and its baseline checks injected
    - a Todo ImplementationTask is scheduled directly to the implementation worker
      with no per-task LLM contract-authoring step
    - the local worker container runs as uid:gid 1000:1000
    - no opencode.json appears at the workspace root
    - the verification baseline (clean install + tests + acceptance) runs after
      implementation
    - a rejected completion receives bounded retry feedback and is accepted
    """
    pytest.importorskip("open_tulid")

    project = _make_runtime_project(
        tmp_path,
        scripted_runtime_worker_image,
        scenario="standard_contract",
    )
    (project.project / "contract.yaml").write_text(STANDARD_CONTRACT, encoding="utf-8")

    try:
        # Project validation must accept the well-formed standard contract.
        validated = _run_tulid(project.root, "validate")
        assert validated.returncode == 0, validated.stdout + validated.stderr
        assert "Vault validation passed" in validated.stdout
        assert "standard_contract." not in validated.stdout

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
            "implementation task to finish with baseline verification and repair",
            timeout=60.0,
        )

        # (1) The Todo implementation task schedules straight to the worker and
        # freezes the project-global contract (no per-task contract authored).
        implement_job = _job_payload_for_transition(project, "ImplementTask")
        assert implement_job["status"] == "accepted"
        contract_meta = implement_job["metadata"]["execution_contract"]
        assert contract_meta["generated_contract"]["schema"] == "tulid.global_contract/v1"
        resolved_ids = [check["id"] for check in contract_meta["resolved_checks"]]
        assert "build" in resolved_ids, resolved_ids
        assert "tests" in resolved_ids, resolved_ids
        surface = contract_meta["generated_contract"]["change_surface"]
        # Command-only contract: no file allowlist is recorded for acceptance.
        assert surface["add"] == []
        assert surface["edit"] == []
        assert surface["forbidden"] == []
        event_store = JsonlEventStore(project.project / "events")
        accepted = [
            event.job_id
            for event in event_store.iter_events()
            if event.event_type == "TransitionAccepted"
        ]
        assert implement_job["job_id"] in accepted
        # No per-task implementation-contract artifact or PrepareExecutionContract
        # worker job exists anywhere in the run.
        assert all(
            "ImplementationContract" not in link
            for link in _task_links(project, "2")
        )

        # (2) local worker ran as uid:gid 1000:1000
        worker_stdout = _job_stdout(implement_job)
        assert "uid=1000 gid=1000" in worker_stdout, worker_stdout

        # (3) no root opencode.json; runtime config stays under .open-tulid/home
        implementation_workspace = Path(str(implement_job["workspace_path"]))
        assert not (implementation_workspace / "opencode.json").exists()
        opencode_config = (
            implementation_workspace / ".open-tulid" / "home" / ".config" / "opencode" / "opencode.json"
        )
        assert not opencode_config.exists()

        # (4) bounded retry feedback: rejected attempt -> repair -> accepted
        assert implement_job["attempts"] == 2
        submissions = implement_job["metadata"]["completion_submissions"]
        assert submissions["implement-task-rejected"]["accepted"] is False
        assert submissions["implement-task-repaired"]["accepted"] is True
        repair_after = implement_job["metadata"]["repair_attempts"]
        assert repair_after <= json.loads(
            (project.project / "contract.yaml").read_text(encoding="utf-8")
            .split("max_attempts:")[1].split("\n")[0].strip()
        )
        rejected_events = [
            event
            for event in JsonlEventStore(project.project / "events").iter_events()
            if event.event_type == "ExecutionCompletionRejected"
        ]
        assert any(event.job_id == implement_job["job_id"] for event in rejected_events)

        # No PrepareExecutionContract jobs were ever created.
        assert all(
            payload.get("transition_id") != "PrepareExecutionContract"
            for payload in _job_payloads(project)
        )
    finally:
        _run_tulid(project.root, "runtime", "stop", "--project", "Agent")
        _print_system_logs(project, capsys)


def _task_links(project, task_id: str) -> tuple[str, ...]:
    task_path = next(iter((project.project / "tasks").glob(f"{task_id}-*.md")))
    links: list[str] = []
    for line in task_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- [[") and stripped.endswith("]]"):
            links.append(stripped[3:-2])
    return tuple(links)
