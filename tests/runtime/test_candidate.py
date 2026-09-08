from __future__ import annotations

import hashlib
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
