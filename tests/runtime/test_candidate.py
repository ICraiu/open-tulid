from __future__ import annotations

import hashlib
import pytest
from pathlib import Path

from open_tulid.runtime.candidate import (
    CANDIDATE_SCHEMA,
    KIND_ADD,
    KIND_DELETE,
    KIND_EDIT,
    capture_candidate,
)
from open_tulid.runtime.repository_facts import (
    BASELINE_MANIFEST_SCHEMA,
    FileManifestEntry,
    BaselineManifest,
    canonical_sha256,
)


@pytest.mark.parametrize("identity", ["../outside", "/outside", ".", "..", ""])
def test_candidate_rejects_path_identities(tmp_path, identity):
    result = capture_candidate(workspace=tmp_path, storage_root=tmp_path / "store",
                               candidate_id=identity, baseline=None)
    assert not result.accepted
    assert result.errors[0].code == "candidate.invalid_identity"


@pytest.mark.parametrize("directory", ["build", "dist", "output", "target", "htmlcov"])
def test_baseline_workspace_and_candidate_preserve_same_source_surface(tmp_path, directory):
    from open_tulid.runtime.repository_facts import capture_repository_snapshot
    from open_tulid.runtime.workspaces import _copy_repo
    repo, workspace = tmp_path / "repo", tmp_path / "workspace"
    source = repo / directory / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("original source")
    baseline = capture_repository_snapshot(repo).snapshot.baseline
    assert [entry.path for entry in baseline.entries] == [f"{directory}/source.txt"]
    workspace.mkdir()
    _copy_repo(repo, workspace)
    assert (workspace / directory / "source.txt").read_text() == "original source"
    captured = capture_candidate(workspace=workspace, storage_root=tmp_path / "candidates",
                                 candidate_id="unchanged", baseline=baseline)
    assert captured.accepted, captured.errors
    assert captured.captured.candidate.changes == ()
    (workspace / directory / "source.txt").write_text("changed source")
    changed = capture_candidate(workspace=workspace, storage_root=tmp_path / "candidates",
                                candidate_id="changed", baseline=baseline)
    assert changed.accepted, changed.errors
    assert [(change.path, change.kind) for change in changed.captured.candidate.changes] == [
        (f"{directory}/source.txt", KIND_EDIT),
    ]


@pytest.mark.parametrize("directory", [False, True])
def test_snapshot_rejects_symlinks_instead_of_sealing_mutable_external_bytes(tmp_path, directory):
    from open_tulid.runtime.repository_facts import capture_repository_snapshot
    from open_tulid.runtime.workspaces import _copy_repo
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    if directory:
        external.mkdir()
        (external / "source.txt").write_text("outside source")
    else:
        external.write_text("outside source")
    (repo / "linked").symlink_to(external, target_is_directory=directory)
    baseline = capture_repository_snapshot(repo)
    assert not baseline.accepted
    assert "symlink" in baseline.errors[0].message
    candidate = capture_candidate(workspace=repo, storage_root=tmp_path / "seals",
                                   candidate_id="linked", baseline=None)
    assert not candidate.accepted
    assert not (tmp_path / "seals").exists()
    with pytest.raises(OSError, match="symlink"):
        _copy_repo(repo, tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()


def test_candidate_never_replaces_existing_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "app.py"
    source.write_text("first")
    kwargs = dict(workspace=workspace, storage_root=tmp_path / "store",
                  candidate_id="complete", baseline=None)
    first = capture_candidate(**kwargs)
    source.write_text("second")
    second = capture_candidate(**kwargs)
    assert first.accepted and not second.accepted
    assert (first.captured.storage_path / "app.py").read_text() == "first"


def test_executable_change_is_verified_and_delivered(tmp_path):
    from open_tulid.runtime.candidate import capture_deliverable_manifest
    from open_tulid.runtime.completion import _candidate_change_plan, _validate_integrated_source
    import shutil
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "run.sh").chmod(0o644)
    baseline = capture_deliverable_manifest(repo)
    workspace = tmp_path / "workspace"
    shutil.copytree(repo, workspace)
    (workspace / "run.sh").chmod(0o755)
    captured = capture_candidate(workspace=workspace, storage_root=tmp_path / "store",
                                 candidate_id="mode", baseline=baseline).captured
    assert captured.manifest.sha256 != baseline.sha256
    assert len(captured.candidate.changes) == 1
    assert captured.candidate.changes[0].after_mode == 0o755
    assert _validate_integrated_source(repo_root=repo, candidate=captured.candidate)
    # The delivery operation must include the mode-only edit.
    plan = _candidate_change_plan(repo_root=repo, candidate_storage=captured.storage_path,
                              changes=captured.candidate.changes)
    assert len(plan) == 1
    shutil.copy2(plan[0]["source_path"], plan[0]["target_path"])
    assert (repo / "run.sh").stat().st_mode & 0o777 == 0o755
    assert not _validate_integrated_source(repo_root=repo, candidate=captured.candidate)


def _baseline(entries: tuple[tuple[str, str, int], ...]) -> BaselineManifest:
    ordered = tuple(
        FileManifestEntry(path=path, sha256=sha256, size=size)
        for path, sha256, size in sorted(entries, key=lambda item: item[0])
    )
    payload = {
        "schema": BASELINE_MANIFEST_SCHEMA,
        "entries": [
            {"path": e.path, "sha256": e.sha256, "size": e.size}
            for e in ordered
        ],
    }
    return BaselineManifest(
        schema=BASELINE_MANIFEST_SCHEMA,
        entries=ordered,
        sha256=canonical_sha256(payload),
    )


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_capture_candidate_computes_authoritative_delta(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "added.txt").write_text("added\n", encoding="utf-8")
    (workspace / "kept.txt").write_text("kept\n", encoding="utf-8")
    (workspace / "edited.txt").write_text("new\n", encoding="utf-8")
    baseline = _baseline((
        ("edited.txt", _digest(b"old\n"), 5),
        ("kept.txt", _digest(b"kept\n"), 5),
        ("removed.txt", _digest(b"gone\n"), 5),
    ))
    storage = tmp_path / "candidates"

    result = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-1",
        baseline=baseline,
        submitted_changed_files=("added.txt",),
    )

    assert result.accepted is True
    assert result.captured is not None
    candidate = result.captured.candidate
    assert candidate.schema == CANDIDATE_SCHEMA
    assert candidate.baseline_sha256 == baseline.sha256
    assert candidate.candidate_id == "cand-1"
    by_kind = {change.kind: change for change in candidate.changes}
    assert by_kind[KIND_ADD].path == "added.txt"
    assert by_kind[KIND_EDIT].path == "edited.txt"
    assert by_kind[KIND_DELETE].path == "removed.txt"
    assert by_kind[KIND_ADD].before_sha256 is None
    assert by_kind[KIND_DELETE].after_sha256 is None
    assert candidate.submitted_discrepancy is not None
    assert "removed.txt" in candidate.submitted_discrepancy
    assert candidate.sha256


def test_capture_candidate_delta_does_not_rely_on_submitted_list(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "omitted.txt").write_text("worker forgot this\n", encoding="utf-8")
    baseline = _baseline(())
    storage = tmp_path / "candidates"

    result = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-omit",
        baseline=baseline,
        submitted_changed_files=("unrelated.txt",),
    )

    assert result.accepted is True
    assert result.captured is not None
    candidate = result.captured.candidate
    assert tuple(change.path for change in candidate.changes) == ("omitted.txt",)
    assert candidate.submitted_discrepancy is not None
    assert "omitted.txt" in candidate.submitted_discrepancy


def test_capture_candidate_supports_genuine_no_change(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("same\n", encoding="utf-8")
    baseline = _baseline((("file.txt", _digest(b"same\n"), 6),))
    storage = tmp_path / "candidates"

    result = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-nc",
        baseline=baseline,
        submitted_changed_files=(),
    )

    assert result.accepted is True
    assert result.captured is not None
    assert result.captured.candidate.changes == ()
    assert result.captured.candidate.submitted_discrepancy is None


def test_capture_candidate_excludes_internal_and_ephemeral_trees(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("value\n", encoding="utf-8")
    for internal in (".git", ".open-tulid", "node_modules", "__pycache__"):
        (workspace / internal).mkdir()
        (workspace / internal / "noise.txt").write_text("noise\n", encoding="utf-8")
    baseline = _baseline((("file.txt", _digest(b"value\n"), 7),))
    storage = tmp_path / "candidates"

    result = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-ex",
        baseline=baseline,
    )

    assert result.accepted is True
    assert result.captured is not None
    assert [entry.path for entry in result.captured.manifest.entries] == ["file.txt"]
    assert not (Path(result.captured.storage_path) / "node_modules").exists()


def test_capture_candidate_retains_binary_bytes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = bytes(range(256)) * 4
    binary = workspace / "bin.dat"
    binary.write_bytes(content)
    baseline = _baseline((("bin.dat", _digest(content), len(content)),))
    storage = tmp_path / "candidates"

    result = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-bin",
        baseline=baseline,
    )

    assert result.accepted is True
    assert result.captured is not None
    assert (result.captured.storage_path / "bin.dat").read_bytes() == content


def test_capture_candidate_seals_snapshot_and_populates_delta(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("v1\n", encoding="utf-8")
    baseline = _baseline(())
    storage = tmp_path / "candidates"

    result = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-seal",
        baseline=baseline,
    )

    assert result.accepted is True
    assert result.captured is not None
    candidate = result.captured.candidate
    assert candidate.candidate_id == "cand-seal"
    assert candidate.manifest_sha256 == result.captured.manifest.sha256
    assert tuple(change.path for change in candidate.changes) == ("a.txt",)


def test_capture_candidate_later_workspace_edit_yields_distinct_seal(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("v1\n", encoding="utf-8")
    baseline = _baseline(())
    storage = tmp_path / "candidates"

    first = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-a",
        baseline=baseline,
    )
    assert first.accepted is True
    assert first.captured is not None
    first_sha = first.captured.candidate.sha256
    first_stored = first.captured.storage_path

    (workspace / "a.txt").write_text("v2\n", encoding="utf-8")
    second = capture_candidate(
        workspace=workspace,
        storage_root=storage,
        candidate_id="cand-b",
        baseline=baseline,
    )
    assert second.accepted is True
    assert second.captured is not None

    assert first.captured.candidate.changes[0].after_sha256 == _digest(b"v1\n")
    assert second.captured.candidate.changes[0].after_sha256 == _digest(b"v2\n")
    assert second.captured.candidate.sha256 != first_sha
    assert (first_stored / "a.txt").read_text(encoding="utf-8") == "v1\n"
