"""Reliability R3 — sealed source and safe file/directory path-type transitions.

The plan's confirmed gap (reliability-remaining-work-plan.md, section 7): a
pre-scan followed by an ordinary copy leaves a change-between-check-and-copy
(TOCTOU) window, and file/directory replacement needs end-to-end proof. These
tests exercise real filesystem transport with external sentinels and injected
faults: a checked regular file or parent directory replaced by a link to an
external sentinel between scanning and copy must cause capture/promotion to
fail without reading external bytes into accepted evidence; file<->directory
transitions deliver the exact intended deletes/adds with safe ordering and
recoverable failure; an unrelated live file blocks a directory->file transition
instead of being removed recursively; and an injected failure across a path-type
operation restarts (via journal recovery) into exactly one accepted transaction
or a named unresolved conflict.
"""

from __future__ import annotations

import hashlib
import subprocess
from types import MappingProxyType
from pathlib import Path
from typing import Any, Mapping

import pytest

from open_tulid.adapters.base import AdapterCapability, LoadProjectResult, ReadTaskResult, WriteResult
from open_tulid.domain import (
    ExecutionJob,
    ExecutionJobStatus,
    ProjectSnapshot,
    RequirementDefinition,
    Task,
    TransitionDefinition,
    WorkflowDefinition,
    StateDefinition,
    TaskTypeDefinition,
)
from open_tulid.runtime import (
    CompletionService,
    CompletionSubmission,
    FileExecutionJobStore,
    JsonlEventStore,
    TransactionJournalStore,
    VerificationResult,
    recover_completion_transactions,
)
from open_tulid.runtime.execution_contracts import (
    compile_standard_execution_contract,
    execution_contract_to_dict,
)

TASK_ID = "01J00000000000000000000001"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class FakeAdapter:
    """In-memory tracker adapter that records board moves (mirrors test_completion)."""

    def __init__(self, task: Task | None = None) -> None:
        self.task = task or _task()
        self.moved_to: str | None = None

    def load_project(self) -> LoadProjectResult:
        return LoadProjectResult(snapshot=ProjectSnapshot(
            project_id="Agent",
            tasks=MappingProxyType({}),
            board_positions=MappingProxyType({}),
        ))

    def read_task(self, task_id: str) -> ReadTaskResult:
        if task_id != self.task.id:
            return ReadTaskResult(task=None)
        state = self.moved_to or self.task.current_state
        return ReadTaskResult(task=Task(
            id=self.task.id, title=self.task.title, path=self.task.path,
            current_state=state, task_type=self.task.task_type,
            dependencies=self.task.dependencies, artifact_links=self.task.artifact_links,
            parent_id=self.task.parent_id, metadata=self.task.metadata, body=self.task.body,
        ))

    def write_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def create_task(self, task: Task) -> WriteResult:
        return WriteResult(path=task.path)

    def move_task(self, task_id: str, state: str) -> WriteResult:
        self.moved_to = state
        return WriteResult(path=state)

    def append_event(self, event: Mapping[str, Any]) -> WriteResult:
        return WriteResult(path="events/test.jsonl")


def _task() -> Task:
    return Task(
        id=TASK_ID, title="Implement thing", path="tasks/thing.md",
        current_state="Todo", task_type="task",
    )


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({
            "Todo": StateDefinition(id="Todo"),
            "CodeReview": StateDefinition(id="CodeReview"),
        }),
        task_types=MappingProxyType({
            "task": TaskTypeDefinition(id="task", requirements_by_state=MappingProxyType({})),
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            "code": TransitionDefinition(
                id="code", task_type="task", from_state="Todo", to_state="CodeReview",
                worker="codex", requires=RequirementDefinition(artifacts=()), transaction=None,
            ),
        }),
    )


class PassingVerifier:
    def verify(self, **kwargs: object) -> VerificationResult:
        return VerificationResult(True)


@pytest.fixture()
def env(tmp_path: Path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    workspace.mkdir()
    output.mkdir()
    assert store.create(ExecutionJob(
        job_id="01J00000000000000000000JOB",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="code",
        worker_id="codex",
        workspace_path=str(workspace),
        metadata={"completion_token": "secret", "output_path": str(output)},
    )).accepted is True
    events = JsonlEventStore(tmp_path / "events")
    journals = TransactionJournalStore(tmp_path / "journals")
    return tmp_path, store, events, journals


def _project(root: Path) -> Path:
    project = root / "project"
    project.mkdir()
    (project / "contract.yaml").write_text('''schema: tulid.contract/v1
commands:
  - name: backend
    argv: [python, -c, "print('ok')"]
''', encoding="utf-8")
    return project


def _seed_running(repo: Path, project: Path, store: FileExecutionJobStore) -> None:
    compiled = compile_standard_execution_contract(
        project_root=project, repo_root=repo, task=_task(), transition=_workflow().transitions["code"],
    ).contract
    assert compiled
    assert store.update_status("01J00000000000000000000JOB", "running", metadata={
        "execution_contract": execution_contract_to_dict(compiled),
        "execution_contract_sha256": compiled.sha256,
    }).accepted is True


def _service(env, repo: Path) -> CompletionService:
    root, store, events, journals = env
    return CompletionService(
        workflow=_workflow(),
        adapter=FakeAdapter(),
        job_store=store,
        event_store=events,
        journal_store=journals,
        repo_root=repo,
        candidate_root=root / "candidates",
        verifier=PassingVerifier(),
    )


# ---------------------------------------------------------------------------
# Case 1: replace a checked regular file (or parent directory) with a link to an
# external sentinel before copy: capture fails, external bytes never become
# accepted evidence, and the sentinel is unchanged.
# ---------------------------------------------------------------------------


def test_capture_rejects_file_swapped_to_external_link(env, monkeypatch):
    import open_tulid.runtime.candidate as candidate_mod

    root, store, events, journals = env
    workspace = root / "src"
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_text("v1\n", encoding="utf-8")
    external = root / "sentinel.txt"
    external.write_text("EXTERNAL", encoding="utf-8")
    real_copy = candidate_mod._copy_deliverables

    def swap_then_copy(source, target, selection=None):
        (source / "a.txt").unlink()
        (source / "a.txt").symlink_to(external)
        return real_copy(source, target, selection)

    monkeypatch.setattr(candidate_mod, "_copy_deliverables", swap_then_copy)

    result = candidate_mod.capture_candidate(
        workspace=workspace, storage_root=root / "seals", candidate_id="cand", baseline=None,
    )
    assert not result.accepted
    assert any(e.code == "candidate.capture_failed" for e in result.errors)

    seal = root / "seals" / "cand"
    if seal.exists():
        assert not (seal / "a.txt").exists()
        contents = [
            text.encode() for p in seal.rglob("*") if p.is_file()
            for text in (p.read_bytes(),)
        ]
        assert b"EXTERNAL" not in b"".join(contents)
    assert external.read_text(encoding="utf-8") == "EXTERNAL"


def test_capture_rejects_parent_dir_swapped_to_external_link(env, monkeypatch):
    import open_tulid.runtime.candidate as candidate_mod
    import open_tulid.runtime.pathops as pathops_mod

    root, store, events, journals = env
    workspace = root / "ws"
    (workspace / "a").mkdir(parents=True)
    (workspace / "a" / "file.txt").write_text("inner", encoding="utf-8")
    external_dir = root / "external"
    external_dir.mkdir()
    # The external dir holds the SAME filename, so a naive path-resolving copy
    # would read its bytes into the storage tree after the directory swap.
    (external_dir / "file.txt").write_text("EXTERNAL-SECRET", encoding="utf-8")
    real_copy = candidate_mod.copy_regular_nofollow

    def swap_then_copy(*, source_root, source_relative, target_root, target_relative, mode=None):
        # The walker already confirmed ``a/file.txt`` as a regular deliverable;
        # NOW replace the parent directory ``a`` with a link to the external dir
        # before the copy re-opens it. This is the change-between-check-and-copy
        # window at the transport boundary.
        (source_root / "a").rename(source_root / "a-real")
        (source_root / "a").symlink_to(external_dir, target_is_directory=True)
        try:
            return real_copy(
                source_root=source_root, source_relative=source_relative,
                target_root=target_root, target_relative=target_relative, mode=mode,
            )
        finally:
            (source_root / "a").unlink()
            (source_root / "a-real").rename(source_root / "a")

    monkeypatch.setattr(candidate_mod, "copy_regular_nofollow", swap_then_copy)

    result = candidate_mod.capture_candidate(
        workspace=workspace, storage_root=root / "seals", candidate_id="cand", baseline=None,
    )
    assert not result.accepted
    assert any(e.code == "candidate.capture_failed" for e in result.errors)
    seal = root / "seals" / "cand"
    assert not ((seal / "a" / "file.txt").exists() if seal.exists() else False)
    # The external sentinel is unchanged; its bytes never became accepted evidence.
    assert (external_dir / "file.txt").read_text(encoding="utf-8") == "EXTERNAL-SECRET"


# ---------------------------------------------------------------------------
# Case 2: repeat a parent-link replacement at promotion and recovery: no external
# write/delete and no success event.
# ---------------------------------------------------------------------------


def test_promotion_parent_link_replacement_no_external_write(env):
    root, store, events, journals = env
    external = root / "external"
    external.mkdir(parents=True)
    (root / "candidate").mkdir(parents=True)
    source = root / "candidate" / "placement.txt"
    source.write_text("new bytes", encoding="utf-8")
    # The admitted repo root becomes a link to an external directory before copy.
    (root / "repo").symlink_to(external, target_is_directory=True)
    target = root / "repo" / "placement.txt"

    service = _service(env, repo=root / "repo")
    result = service._apply_effect({
        "type": "promote_changed_file",
        "source_path": str(source),
        "target_path": str(target),
        "expected_after_sha256": _digest(b"new bytes"),
    })
    assert not result.accepted
    assert not (external / "placement.txt").exists()


def test_recovery_parent_link_replacement_no_external_write(env):
    root, store, events, journals = env
    (root / "repo").mkdir(parents=True)
    (root / "repo" / "_keep.txt").write_text("keep", encoding="utf-8")
    source = root / "sealed" / "placement.txt"
    source.parent.mkdir(parents=True)
    source.write_text("new bytes", encoding="utf-8")
    target = root / "repo" / "placement.txt"

    journals.prepare(journal_id="link-crash", project_id="Agent", task_id=TASK_ID,
        transition_id="code", effects=(
            {"type": "promote_changed_file", "source_path": str(source),
             "target_path": str(target),
             "expected_before_sha256": None,
             "expected_after_sha256": _digest(b"new bytes")},
            {"type": "move_task", "task_id": TASK_ID, "to_state": "CodeReview"},
        ), events=())

    service = CompletionService(
        workflow=_workflow(), adapter=FakeAdapter(), job_store=store,
        event_store=events, journal_store=journals, repo_root=root / "repo",
    )
    (root / "repo").rename(root / "repo-real")
    external = root / "external"
    external.mkdir()
    (root / "repo").symlink_to(external, target_is_directory=True)
    try:
        recovered = recover_completion_transactions(
            service=service, event_store=events, journal_store=journals
        )
    finally:
        (root / "repo").unlink()
        (root / "repo-real").rename(root / "repo")

    # No success event and no external write.
    assert recovered == ()
    assert not (external / "placement.txt").exists()
    assert journals.load("link-crash").status.value == "prepared"


# ---------------------------------------------------------------------------
# Case 3: baseline `a/b.txt` becomes regular file `a`; inverse replaces file `a`
# with `a/b.txt`: the exact intended deletes/adds are verified and delivered.
# ---------------------------------------------------------------------------


def _full_submit(env, repo: Path, workspace_setup) -> CompletionService:
    root, store, events, journals = env
    # The seeded job's workspace is ``root/workspace``; place candidate files
    # directly in it so the authoritative delta path stays repo-relative.
    workspace = root / "workspace"
    workspace_setup(workspace)
    _seed_running(repo, _project(root), store)
    service = _service(env, repo)
    result = service.submit(
        job_id="01J00000000000000000000JOB",
        token="secret",
        submission=CompletionSubmission(summary="done", changed_files=(),)
    )
    return result


def test_dir_to_file_transition_delivered(env):
    root, store, events, journals = env
    repo = root / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "a" / "b.txt").write_text("b\n", encoding="utf-8")

    def workspace_setup(ws: Path):
        (ws / "a").write_text("now-a-file\n", encoding="utf-8")

    result = _full_submit(env, repo, workspace_setup)
    assert result.accepted is True
    assert (repo / "a").is_file()
    assert (repo / "a").read_text(encoding="utf-8") == "now-a-file\n"
    assert not (repo / "a" / "b.txt").exists()


def test_file_to_dir_transition_delivered(env):
    root, store, events, journals = env
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "a").write_text("was-a-file\n", encoding="utf-8")

    def workspace_setup(ws: Path):
        (ws / "a").mkdir(parents=True)
        (ws / "a" / "b.txt").write_text("inner\n", encoding="utf-8")

    result = _full_submit(env, repo, workspace_setup)
    assert result.accepted is True
    assert (repo / "a").is_dir()
    assert (repo / "a" / "b.txt").read_text(encoding="utf-8") == "inner\n"
    # The old regular file is gone (the directory replaced it exactly).
    assert (repo / "a").is_dir()
    assert not (repo / "a").is_file()


# ---------------------------------------------------------------------------
# Case 4: unrelated live file under a directory the delivery would remove ->
# block, preserve the file, never remove the directory recursively.
# ---------------------------------------------------------------------------


def test_dir_to_file_blocks_on_unrelated_live_file(env):
    root, store, events, journals = env
    repo = root / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "a" / "unrelated.txt").write_text("user work", encoding="utf-8")
    source = root / "sealed" / "a"
    source.parent.mkdir(parents=True)
    source.write_text("file-replacing-dir", encoding="utf-8")

    service = _service(env, repo)
    result = service._apply_effect({
        "type": "promote_changed_file",
        "source_path": str(source),
        "target_path": str(repo / "a"),
        "expected_after_sha256": _digest(b"file-replacing-dir"),
    })
    assert not result.accepted
    assert any(e.code == "changed_file.promotion_failed" for e in result.errors)
    assert (repo / "a" / "unrelated.txt").read_text(encoding="utf-8") == "user work"
    assert (repo / "a").is_dir()


# ---------------------------------------------------------------------------
# Case 5: toggle executable mode, deliver an empty file, change binary bytes:
# target identity equals the verified candidate.
# ---------------------------------------------------------------------------


def test_transition_promotion_identity_matches_candidate(env):
    root, store, events, journals = env
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "tool.bin").write_bytes(b"\x00\x01\x02")

    def workspace_setup(ws: Path):
        tool = ws / "tool.bin"
        tool.write_bytes(b"")
        tool.chmod(0o755)
        (ws / "empty").write_bytes(b"")

    result = _full_submit(env, repo, workspace_setup)
    assert result.accepted is True
    assert (repo / "tool.bin").read_bytes() == b""
    assert (repo / "tool.bin").stat().st_mode & 0o777 == 0o755
    assert (repo / "empty").read_bytes() == b""
    load = store.get("01J00000000000000000000JOB")
    assert load.job is not None
    logged = [item for item in load.job.metadata["promoted_files"]
              if item.get("type") == "promote_changed_file"]
    tool_log = next(item for item in logged if item["target_path"].endswith("tool.bin"))
    assert tool_log["expected_after_mode"] == 0o755
    assert tool_log["expected_after_sha256"] == _digest(b"")


# ---------------------------------------------------------------------------
# Case 7: inject a failure between path-type operations; restart converges to
# exactly one accepted transaction or a named unresolved conflict.
# ---------------------------------------------------------------------------


def test_injected_crash_between_path_ops_converges_on_restart(env, monkeypatch):
    """A crash right after the delete (before the promote) leaves a PREPARED
    journal; restart recovery must replay only the missing effect and settle
    exactly one accepted transaction, delivering the exact intended path-type
    transition without duplicating the delete."""
    import open_tulid.runtime.completion as completion_mod

    root, store, events, journals = env
    repo = root / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "a" / "b.txt").write_text("b\n", encoding="utf-8")

    def workspace_setup(ws: Path):
        (ws / "a").write_text("now-a-file\n", encoding="utf-8")
    workspace = root / "workspace"
    workspace_setup(workspace)
    _seed_running(repo, _project(root), store)

    original = completion_mod._promote_changed_nofollow
    calls = {"n": 0}

    def crashing_promote(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # A process crash is a BaseException, so the PREPARED journal is left
            # unreconciled and the already-applied delete is not compensated.
            raise KeyboardInterrupt("simulated crash after delete, before promote")
        return original(**kwargs)

    # The promote runs after the delete in the effect order; crash it to leave a
    # half-applied (delete-done, promote-undone) PREPARED transaction.
    monkeypatch.setattr(completion_mod, "_promote_changed_nofollow", crashing_promote)

    service = _service(env, repo)
    with pytest.raises(KeyboardInterrupt):
        service.submit(
            job_id="01J00000000000000000000JOB",
            token="secret",
            submission=CompletionSubmission(summary="done", changed_files=()),
        )
    # A crash: the PREPARED journal remains, the delete already landed.
    assert not (repo / "a" / "b.txt").exists()
    assert (repo / "a").is_dir()
    assert (repo / "a").is_dir()

    # Restart: recovery replays only the missing promote and settles exactly one
    # accepted transaction; the directory -> file transition is delivered once.
    service2 = _service(env, repo)
    recovered = recover_completion_transactions(
        service=service2, event_store=events, journal_store=journals
    )
    assert len(recovered) == 1
    load = store.get("01J00000000000000000000JOB")
    assert load.job is not None
    assert str(getattr(load.job.status, "value", load.job.status)) == "accepted"
    assert (repo / "a").is_file()
    assert (repo / "a").read_text(encoding="utf-8") == "now-a-file\n"
    assert not (repo / "a" / "b.txt").exists()
