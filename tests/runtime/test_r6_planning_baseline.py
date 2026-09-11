from __future__ import annotations

import hashlib
from pathlib import Path

from open_tulid.runtime import WorkspacePreparer
from open_tulid.runtime.execution_contracts import FrozenContextFile
from open_tulid.runtime.planning_inputs import (
    freeze_planning_inputs,
    frozen_repository_baseline_from_snapshot,
    load_planning_inputs,
)
from open_tulid.runtime.repository_facts import (
    capture_repository_snapshot,
    source_selection_to_dict,
)
from open_tulid.runtime.workspaces import WorkspacePreparer as _WorkspacePreparer


def _task_dict():
    from open_tulid.domain import Task
    return Task(
        id="01J00000000000000000000001",
        title="Plan",
        path="tasks/plan.md",
        current_state="ReadyForBreakdown",
        task_type="QuestionRound",
        body="Break down the work.",
    )


def _transition():
    from open_tulid.domain import RequirementDefinition, TransitionDefinition
    return TransitionDefinition(
        id="BreakDownImplementationSpec",
        task_type="QuestionRound",
        from_state="ReadyForBreakdown",
        to_state="Done",
        worker="codex_breakdown",
        requires=RequirementDefinition(),
        transaction=None,
    )


def _job(metadata=None):
    from open_tulid.domain import ExecutionJob
    return ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id="01J00000000000000000000001",
        transition_id="BreakDownImplementationSpec",
        worker_id="codex_breakdown",
        workspace_path="/tmp/r6-workspace",
        metadata=metadata or {},
    )


def _init_git(repo: Path) -> None:
    import subprocess
    for cmd in (("init", "-q"), ("add", "--all"), ("commit", "-qm", "baseline")):
        subprocess.run(("git", *cmd), cwd=repo, capture_output=True, check=False)


def _cloned_job(job, workspace: Path):
    return type(job)(
        job_id=job.job_id, project_id=job.project_id, task_id=job.task_id,
        transition_id=job.transition_id, worker_id=job.worker_id,
        workspace_path=str(workspace), metadata=dict(job.metadata),
    )


def _freeze(repo: Path | None, prompt: str = "Plan prompt."):
    snapshot = capture_repository_snapshot(repo) if repo is not None else None
    baseline = frozen_repository_baseline_from_snapshot(snapshot)
    return freeze_planning_inputs(
        _task_dict(),
        _transition(),
        prompt,
        (),
        repository_baseline=baseline,
    )


def test_planning_inputs_freeze_and_load_round_trip_repository_baseline(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.js").write_text("console.log('x')\n", encoding="utf-8")

    _init_git(repo)
    frozen = _freeze(repo)
    assert frozen["repository_baseline"] is not None
    assert frozen["repository_baseline"]["schema"] == "tulid.planning-repository-baseline/v1"
    assert frozen["repository_baseline"]["baseline_manifest_sha256"]

    loaded = load_planning_inputs(_job(metadata={"planning_inputs": frozen}))
    assert loaded is not None
    assert loaded.repository_baseline is not None
    assert loaded.repository_baseline.baseline_manifest_sha256 == frozen["repository_baseline"]["baseline_manifest_sha256"]
    assert loaded.repository_baseline.source_selection is not None
    assert loaded.repository_baseline.source_selection.git is True
    assert loaded.repository_baseline.base_commit is not None


def test_planning_inputs_without_repo_have_no_baseline(tmp_path: Path):
    frozen = _freeze(None)
    assert frozen["repository_baseline"] is None
    loaded = load_planning_inputs(_job(metadata={"planning_inputs": frozen}))
    assert loaded is not None
    assert loaded.repository_baseline is None


def test_planning_inputs_digest_rejects_tampered_baseline(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _init_git(repo)
    frozen = _freeze(repo)
    tampered = {**frozen, "repository_baseline": {**frozen["repository_baseline"], "base_commit": "0" * 40}}
    import pytest as _pytest
    with _pytest.raises(ValueError, match="digest mismatch"):
        load_planning_inputs(_job(metadata={"planning_inputs": tampered}))


def test_prepare_uses_frozen_source_selection_and_passes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("ignored\n", encoding="utf-8")
    # A tracked file under a declared-cache name survives because the frozen Git
    # selection retains tracked regular files.
    (repo / "node_modules").mkdir()
    tracked = repo / "node_modules" / "project-owned.js"
    tracked.write_text("module.exports = 1\n", encoding="utf-8")
    _init_git(repo)

    frozen = _freeze(repo)
    selection_dict = frozen["repository_baseline"]["source_selection"]
    assert selection_dict["mode"] == "git"

    workspace = tmp_path / "workspace"
    job = _cloned_job(_job(metadata={"planning_inputs": frozen}), workspace)
    result = WorkspacePreparer(repo_root=repo).prepare(
        job=job, task=_task_dict(), transition=_transition(),
    )
    assert result.accepted, result.error
    assert (workspace / "node_modules" / "project-owned.js").is_file()


def test_prepare_blocks_on_stale_baseline_after_repo_edit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("one\n", encoding="utf-8")

    frozen = _freeze(repo)
    workspace = tmp_path / "workspace"
    job = _cloned_job(_job(metadata={"planning_inputs": frozen}), workspace)

    # Change the live repository after the planning job was admitted.
    (repo / "a.txt").write_text("two\n", encoding="utf-8")

    result = WorkspacePreparer(repo_root=repo).prepare(
        job=job, task=_task_dict(), transition=_transition(),
    )
    assert not result.accepted
    assert result.error is not None
    assert result.error.code == "workspace.baseline_mismatch"
    assert "changed after this planning job" in result.error.message


def test_prepare_after_identical_repo_reuse_passes_exactly_once(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("same\n", encoding="utf-8")

    frozen = _freeze(repo)
    workspace = tmp_path / "workspace"
    job = _cloned_job(_job(metadata={"planning_inputs": frozen}), workspace)
    result = WorkspacePreparer(repo_root=repo).prepare(
        job=job, task=_task_dict(), transition=_transition(),
    )
    assert result.accepted, result.error
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "same\n"
