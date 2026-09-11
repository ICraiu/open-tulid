"""Sealed candidate capture for a completion submission (plan 5, step 5B).

When a completion submission passes authentication and deduplication, the
submitted workspace is sealed into a Tulid-owned snapshot outside the active
worker mount. The snapshot is checked for stability with before/after manifest
comparisons before it becomes the verification target, so later workspace
mutations cannot ride along with already-known-good bytes.

The authoritative baseline-to-candidate delta is computed from the manifests,
never from the worker's submitted ``changed_files`` list. A submitted list may
explain intent and a stale/incomplete list produces a discrepancy note, but it
cannot cause silent partial transport.

Only 5B is implemented here: capture the stable candidate, seal it, compute the
delta, support a genuine no-change candidate, and expose the candidate identity.
Promotion, repository-lane acquisition, and acceptance transactions are owned by
later steps (5C-5F).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from open_tulid.domain import DomainError
from open_tulid.runtime.pathops import (
    copy_regular_nofollow,
    regular_file_id,
)
from open_tulid.runtime.repository_facts import (
    BASELINE_MANIFEST_SCHEMA,
    FileManifestEntry,
    BaselineManifest,
    SourceSelection,
    canonical_sha256,
    discover_source_selection,
    iter_deliverable_files,
    source_selection_sha256,
    refresh_workspace_selection,
    source_selection_to_dict,
    source_selection_from_dict,
)


CANDIDATE_SCHEMA = "tulid.candidate/v1"

# Change kinds modeled on the plan's change entry vocabulary. Renames are
# represented safely as delete+add; rename detection is explanatory and never
# determines whether bytes are transported.
KIND_ADD = "add"
KIND_EDIT = "edit"
KIND_DELETE = "delete"


@dataclass(frozen=True)
class CandidateChange:
    kind: str
    path: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    before_mode: int | None = None
    after_mode: int | None = None


@dataclass(frozen=True)
class Candidate:
    schema: str
    candidate_id: str
    baseline_sha256: str | None
    manifest_sha256: str
    storage_path: str
    changes: tuple[CandidateChange, ...]
    submitted_changed_files: tuple[str, ...] = ()
    submitted_discrepancy: str | None = None
    # The source-selection rule inputs the candidate was sealed under (R2). The
    # candidate identity binds these rule inputs; verification and delivery use
    # the identical selection so a rule can never differ across transport.
    source_selection: SourceSelection | None = None
    source_selection_sha256: str | None = None
    sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "baseline_sha256": self.baseline_sha256,
            "manifest_sha256": self.manifest_sha256,
            "storage_path": self.storage_path,
            "changes": [
                {
                    "kind": change.kind,
                    "path": change.path,
                    "before_sha256": change.before_sha256,
                    "after_sha256": change.after_sha256,
                    "before_mode": change.before_mode,
                    "after_mode": change.after_mode,
                }
                for change in self.changes
            ],
            "submitted_changed_files": list(self.submitted_changed_files),
            "submitted_discrepancy": self.submitted_discrepancy,
            "source_selection": (
                source_selection_to_dict(self.source_selection)
                if self.source_selection is not None
                else None
            ),
            "source_selection_sha256": self.source_selection_sha256,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class CapturedCandidate:
    candidate: Candidate
    storage_path: Path
    manifest: BaselineManifest

    @property
    def identity(self) -> str:
        return self.candidate.sha256


@dataclass(frozen=True)
class CaptureCandidateResult:
    captured: CapturedCandidate | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


def capture_candidate(
    *,
    workspace: Path,
    storage_root: Path,
    candidate_id: str,
    baseline: BaselineManifest | None,
    submitted_changed_files: tuple[str, ...] = (),
    selection: SourceSelection | None = None,
) -> CaptureCandidateResult:
    """Seal a stable snapshot of ``workspace`` into ``storage_root``.

    A copy plus stable before/after manifest checks is used instead of assuming
    copying a live directory is atomic. If the workspace changes while the copy
    runs, or the stored copy does not match the workspace, the candidate is
    rejected with a retriable error and the original workspace is preserved.

    ``selection`` applies the frozen source-selection rule. When omitted it is
    discovered from the live workspace, which for a worker workspace (no
    ``.git``) selects the documented non-Git rule.
    """
    storage_root = Path(storage_root)
    if not candidate_id or candidate_id in {".", ".."} or Path(candidate_id).name != candidate_id:
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.invalid_identity", "Candidate identity must be a single path component.",
        ),))
    storage = storage_root / candidate_id
    if selection is None:
        selection = discover_source_selection(workspace)
    else:
        # The frozen tracked source identity travels; Git ignore decision layers
        # are re-read from the tree so a ``.gitignore`` edit inside this candidate
        # is applied and bound to its identity (row 5).
        selection = refresh_workspace_selection(selection, workspace)
    selection_sha256 = source_selection_sha256(selection)
    try:
        pre = capture_deliverable_manifest(workspace, selection)
    except OSError as exc:
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.capture_failed",
            f"Cannot scan the workspace for candidate capture: {exc}",
            location=str(workspace),
        ),))
    try:
        storage_root.mkdir(parents=True, exist_ok=True)
        # Exclusive creation preserves sealed evidence, including during races.
        storage.mkdir(exist_ok=False)
        _copy_deliverables(workspace, storage, selection)
    except OSError as exc:
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.capture_failed",
            f"Cannot copy the candidate snapshot: {exc}",
            location=str(storage),
        ),))
    try:
        post = capture_deliverable_manifest(workspace, selection)
        stored = capture_deliverable_manifest(storage, selection)
    except OSError as exc:
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.capture_failed",
            f"Cannot re-scan the captured candidate: {exc}",
            location=str(storage),
        ),))

    if post.sha256 != pre.sha256:
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.unstable",
            (
                "The workspace changed while the candidate snapshot was being "
                "captured. Retry the submission; the workspace is preserved."
            ),
            location=str(workspace),
        ),))
    if stored.sha256 != post.sha256:
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.capture_failed",
            (
                "The stored candidate snapshot does not match the workspace. "
                "Retry the submission; the workspace is preserved."
            ),
            location=str(storage),
        ),))

    changes = _compute_delta(baseline, post)
    delta_paths = {change.path for change in changes}
    submitted = tuple(sorted({Path(p).as_posix() for p in submitted_changed_files}))
    discrepancy = _discrepancy_note(submitted, delta_paths)
    candidate = Candidate(
        schema=CANDIDATE_SCHEMA,
        candidate_id=candidate_id,
        baseline_sha256=baseline.sha256 if baseline is not None else None,
        manifest_sha256=post.sha256,
        storage_path=str(storage),
        changes=changes,
        submitted_changed_files=submitted,
        submitted_discrepancy=discrepancy,
        source_selection=selection,
        source_selection_sha256=selection_sha256,
    )
    sealed = Candidate(
        schema=candidate.schema,
        candidate_id=candidate.candidate_id,
        baseline_sha256=candidate.baseline_sha256,
        manifest_sha256=candidate.manifest_sha256,
        storage_path=candidate.storage_path,
        changes=candidate.changes,
        submitted_changed_files=candidate.submitted_changed_files,
        submitted_discrepancy=candidate.submitted_discrepancy,
        source_selection=candidate.source_selection,
        source_selection_sha256=candidate.source_selection_sha256,
        sha256=canonical_sha256(_candidate_body(candidate)),
    )
    try:
        _write_json(storage_root / f"{candidate_id}.candidate.json", sealed.to_dict())
    except OSError:
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.capture_failed",
            "Cannot persist the sealed candidate record.",
            location=str(storage_root),
        ),))
    if post.sha256 != capture_deliverable_manifest(workspace, selection).sha256:
        # The workspace mutated after the sealed snapshot was confirmed but
        # before the record was written; do not accept a now-stale seal.
        return CaptureCandidateResult(errors=(_candidate_error(
            "candidate.unstable",
            (
                "The workspace changed while the candidate was being sealed. "
                "Retry the submission; the workspace is preserved."
            ),
            location=str(workspace),
        ),))
    return CaptureCandidateResult(captured=CapturedCandidate(
        candidate=sealed,
        storage_path=storage,
        manifest=post,
    ))


def capture_deliverable_manifest(root: Path, selection: SourceSelection | None = None) -> BaselineManifest:
    """Hash the authoritative deliverable surface used by capture and verification."""
    entries: list[FileManifestEntry] = []
    for path in iter_deliverable_files(root, selection):
        relative = path.relative_to(root).as_posix()
        sha256, size, mode = _nofollow_file_id(root, relative)
        entries.append(FileManifestEntry(
            path=relative,
            sha256=sha256,
            size=size,
            mode=mode,
        ))
    ordered = tuple(sorted(entries, key=lambda entry: entry.path))
    payload = {
        "schema": BASELINE_MANIFEST_SCHEMA,
        "entries": [
            {"path": entry.path, "sha256": entry.sha256, "size": entry.size, "mode": entry.mode}
            for entry in ordered
        ],
    }
    return BaselineManifest(
        schema=BASELINE_MANIFEST_SCHEMA,
        entries=ordered,
        sha256=canonical_sha256(payload),
    )


def iter_deliverable_files(root: Path, selection: SourceSelection | None = None):
    """Walk source deterministically using the one source-selection rule.

    Delegates to the repository_facts walk so baseline, workspace, seal, verifier,
    and target all consume the identical selection.
    """
    from open_tulid.runtime.repository_facts import iter_deliverable_files as _walk
    yield from _walk(root, selection)


def _copy_deliverables(source: Path, target: Path, selection: SourceSelection | None = None) -> None:
    # Copy only the deliverable surface; symlinks and unsupported entries raise
    # through the iterator so they fail closed before external bytes are read.
    #
    # R3: each copied file is re-opened with O_NOFOLLOW from a O_NOFOLLOW-walked
    # parent directory fd. A checked regular file (or its parent directory)
    # swapped to a link pointing outside the source between the scan and the copy
    # is rejected instead of dereferenced, so external sentinel bytes are never
    # read into the sealed snapshot.
    if selection is None:
        selection = discover_source_selection(source)
    for path in iter_deliverable_files(source, selection):
        relative = path.relative_to(source).as_posix()
        copy_regular_nofollow(
            source_root=source,
            source_relative=relative,
            target_root=target,
            target_relative=relative,
        )


def _nofollow_file_id(root: Path, relative: str) -> tuple[str, int, int]:
    return regular_file_id(root, relative)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compute_delta(
    baseline: BaselineManifest | None,
    candidate: BaselineManifest,
) -> tuple[CandidateChange, ...]:
    before = {} if baseline is None else {entry.path: entry for entry in baseline.entries}
    after = {entry.path: entry for entry in candidate.entries}
    changes: list[CandidateChange] = []
    for path in sorted(set(after) - set(before)):
        entry = after[path]
        changes.append(CandidateChange(KIND_ADD, path, None, entry.sha256, None, entry.mode))
    for path in sorted(set(before) - set(after)):
        entry = before[path]
        changes.append(CandidateChange(KIND_DELETE, path, entry.sha256, None, entry.mode, None))
    for path in sorted(set(before) & set(after)):
        if (before[path].sha256 != after[path].sha256 or
                (before[path].mode is not None and before[path].mode != after[path].mode)):
            changes.append(CandidateChange(
                KIND_EDIT,
                path,
                before[path].sha256,
                after[path].sha256,
                before[path].mode,
                after[path].mode,
            ))
    return tuple(changes)


def _discrepancy_note(
    submitted: tuple[str, ...],
    delta_paths: set[str],
) -> str | None:
    submitted_set = set(submitted)
    if submitted_set == delta_paths:
        return None
    missing = sorted(delta_paths - submitted_set)
    extra = sorted(submitted_set - delta_paths)
    parts = []
    if missing:
        parts.append("missing from the submitted list: " + ", ".join(missing))
    if extra:
        parts.append("submitted but not changed: " + ", ".join(extra))
    return "Submitted changed-file list differs from the authoritative candidate delta (" + "; ".join(parts) + ")."


def _candidate_body(candidate: Candidate) -> dict[str, object]:
    return {
        "schema": candidate.schema,
        "candidate_id": candidate.candidate_id,
        "baseline_sha256": candidate.baseline_sha256,
        "manifest_sha256": candidate.manifest_sha256,
        "changes": [
            {
                "kind": change.kind,
                "path": change.path,
                "before_sha256": change.before_sha256,
                "after_sha256": change.after_sha256,
                "before_mode": change.before_mode,
                "after_mode": change.after_mode,
            }
            for change in candidate.changes
        ],
        "submitted_changed_files": list(candidate.submitted_changed_files),
        "submitted_discrepancy": candidate.submitted_discrepancy,
        "source_selection_sha256": candidate.source_selection_sha256,
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _candidate_error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
