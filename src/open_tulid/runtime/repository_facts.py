from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from open_tulid.domain import DomainError


REPOSITORY_FACTS_SCHEMA = "tulid.repository-facts/v1"
BASELINE_MANIFEST_SCHEMA = "tulid.baseline-manifest/v1"
REPOSITORY_IDENTITY_SCHEMA = "tulid.repository-identity/v1"
SOURCE_SELECTION_SCHEMA = "tulid.source-selection/v1"
SOURCE_SELECTION_VERSION = 1

# Reserved namespaces that are never application delivery: Tulid's own internal
# workspace area (.open-tulid) and Git metadata (.git). A repository that tries
# to track or ship source under the reserved Tulid namespace is diagnosed rather
# than silently transported.
RESERVED_DIRECTORY_NAMES = frozenset({".git", ".open-tulid"})

# Declared ephemeral cache/output names. For Git sources these prune ONLY
# untracked content; a tracked regular file under such a name is retained. For
# non-Git sources they form the documented single exclusion rule.
DECLARED_CACHE_DIRECTORY_NAMES = frozenset({
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
})

EXCLUDED_DIRECTORY_NAMES = RESERVED_DIRECTORY_NAMES | DECLARED_CACHE_DIRECTORY_NAMES

KNOWN_MANIFESTS = frozenset({
    "Cargo.toml",
    "Gemfile",
    "go.mod",
    "package.json",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
})

KNOWN_ENTRYPOINT_FILES = frozenset({
    "Justfile",
    "Makefile",
    "noxfile.py",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
})

MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s|$)")


@dataclass(frozen=True)
class FileManifestEntry:
    path: str
    sha256: str
    size: int
    mode: int | None = None


@dataclass(frozen=True)
class BaselineManifest:
    schema: str
    entries: tuple[FileManifestEntry, ...]
    sha256: str


@dataclass(frozen=True)
class RepositoryFacts:
    schema: str
    repository_available: bool
    git_repository: bool
    base_commit: str | None
    dirty: bool | None
    top_level_entries: tuple[str, ...]
    manifests: tuple[str, ...]
    detected_entrypoints: tuple[str, ...]
    file_count: int
    total_bytes: int
    sha256: str
    # Frozen source-selection rule carried across baseline -> workspace -> seal
    # -> verifier -> target (R2). ``None`` means a legacy manifest with no frozen
    # rule; such manifests preserve their historical interpretation.
    source_selection: SourceSelection | None = None


@dataclass(frozen=True)
class RepositorySnapshot:
    facts: RepositoryFacts
    baseline: BaselineManifest


@dataclass(frozen=True)
class RepositorySnapshotResult:
    snapshot: RepositorySnapshot | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class IgnoreFile:
    """One frozen ``.gitignore`` decision layer.

    ``base_dir`` is the repository-relative directory that owns this
    ``.gitignore`` (``""`` for the root). The raw ``content`` is retained so the
    identical deterministic ignore rule can be re-applied in workspaces that have
    no ``.git``, and its digest binds the candidate identity to its rule inputs.
    """

    base_dir: str
    content: str
    sha256: str


@dataclass(frozen=True)
class SourceSelection:
    """The single, versioned source-selection rule carried across transport.

    ``mode`` is either ``"git"`` or ``"non_git"``. For Git sources the complete
    rule is: retain tracked regular files even under cache-like names; retain
    permitted non-ignored new source; exclude untracked declared caches and
    Git-ignored new material. ``tracked_paths`` (frozen at admission, where the
    live ``.git`` exists) is the repository source identity that lets the same
    rule be applied again in workspaces without ``.git``. ``ignore_files`` are the
    commit-time Git ignore decision layers re-read from each tree so a
    ``.gitignore`` edit inside one candidate is applied deterministically there.
    A non-Git project uses one documented, versioned rule: exclude reserved and
    declared-cache directory names. This describes repository source, never a
    task-specific edit allowlist.
    """

    schema: str
    mode: str
    version: int = SOURCE_SELECTION_VERSION
    tracked_paths: tuple[str, ...] = ()
    ignore_files: tuple[IgnoreFile, ...] = ()

    @property
    def git(self) -> bool:
        return self.mode == "git"


def source_selection_sha256(selection: SourceSelection) -> str:
    return canonical_sha256(source_selection_to_dict(selection))


def refresh_workspace_selection(frozen: SourceSelection, root: Path) -> SourceSelection:
    """Re-read the effective decision layers from a tree that has no ``.git``.

    The frozen ``tracked_paths`` (repository source identity) travel unchanged,
    but Git ignore decisions are re-read from the tree's own ``.gitignore`` files
    so a ``.gitignore`` edit inside one candidate is applied deterministically
    there and bound to that candidate's identity. Non-Git rules have no ignore
    layers and are returned unchanged.
    """
    if not frozen.git:
        return frozen
    return SourceSelection(
        schema=frozen.schema,
        mode=frozen.mode,
        version=frozen.version,
        tracked_paths=frozen.tracked_paths,
        ignore_files=_read_ignore_files(root),
    )


def source_selection_to_dict(selection: SourceSelection) -> dict[str, object]:
    return {
        "schema": selection.schema,
        "mode": selection.mode,
        "version": selection.version,
        "tracked_paths": list(selection.tracked_paths),
        "ignore_files": [
            {
                "base_dir": ignore_file.base_dir,
                "content": ignore_file.content,
                "sha256": ignore_file.sha256,
            }
            for ignore_file in selection.ignore_files
        ],
    }


def source_selection_from_dict(
    payload: Mapping[str, object],
) -> SourceSelection:
    mode = str(payload.get("mode", "non_git"))
    if mode not in {"git", "non_git"}:
        raise ValueError("source selection mode must be 'git' or 'non_git'")
    tracked = _string_tuple(payload.get("tracked_paths", ()))
    raw_ignores = payload.get("ignore_files", ())
    ignore_files: list[IgnoreFile] = []
    if isinstance(raw_ignores, Sequence) and not isinstance(raw_ignores, (str, bytes, bytearray)):
        for item in raw_ignores:
            if not isinstance(item, Mapping):
                continue
            base_dir = str(item.get("base_dir", ""))
            content = str(item.get("content", ""))
            ignore_files.append(IgnoreFile(
                base_dir=base_dir,
                content=content,
                sha256=str(item.get("sha256", "")),
            ))
    return SourceSelection(
        schema=str(payload.get("schema", SOURCE_SELECTION_SCHEMA)),
        mode=mode,
        version=int(payload.get("version", SOURCE_SELECTION_VERSION)),
        tracked_paths=tuple(str(p) for p in tracked),
        ignore_files=tuple(ignore_files),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def capture_repository_snapshot(
    repo_root: Path | None,
    selection: SourceSelection | None = None,
) -> RepositorySnapshotResult:
    if repo_root is None:
        baseline = _baseline_manifest(())
        facts = _repository_facts(
            repository_available=False,
            git_repository=False,
            base_commit=None,
            dirty=None,
            top_level_entries=(),
            manifests=(),
            detected_entrypoints=(),
            baseline=baseline,
        )
        return RepositorySnapshotResult(snapshot=RepositorySnapshot(
            facts=facts,
            baseline=baseline,
        ))

    root = repo_root.resolve()
    if not root.is_dir():
        return RepositorySnapshotResult(errors=(DomainError(
            code="repository.missing",
            message=f"Repository root does not exist: {root}",
            location=str(root),
        ),))

    # A workspace re-applies the frozen rule (it has no ``.git``); a live source
    # repository discovers it and diagnoses reserved-namespace misuse instead of
    # silently claiming to transport Tulid's internal ``.open-tulid`` area.
    if selection is None:
        selection = discover_source_selection(root)
        reserved = _reserved_namespace_violation(root, selection)
        if reserved is not None:
            return RepositorySnapshotResult(errors=(DomainError(
                code="repository.reserved_namespace",
                message=reserved,
                location=str(root),
            ),))

    entries: list[FileManifestEntry] = []
    try:
        for path in iter_deliverable_files(root, selection):
            entries.append(FileManifestEntry(
                path=path.relative_to(root).as_posix(),
                sha256=_file_sha256(path),
                size=path.stat().st_size,
                mode=path.stat().st_mode & 0o777,
            ))
        top_level_entries = tuple(sorted(
            path.name
            for path in root.iterdir()
            if path.name not in EXCLUDED_DIRECTORY_NAMES
        ))
    except OSError as exc:
        return RepositorySnapshotResult(errors=(DomainError(
            code="repository.scan_failed",
            message=f"Cannot scan repository: {exc}",
            location=str(root),
        ),))

    baseline = _baseline_manifest(tuple(entries))
    git_repository, base_commit, dirty = _git_facts(root)
    manifests = tuple(sorted(
        entry.path
        for entry in baseline.entries
        if Path(entry.path).name in KNOWN_MANIFESTS
    ))
    entrypoints = _detect_entrypoints(root, baseline.entries)
    facts = _repository_facts(
        repository_available=True,
        git_repository=git_repository,
        base_commit=base_commit,
        dirty=dirty,
        top_level_entries=top_level_entries,
        manifests=manifests,
        detected_entrypoints=entrypoints,
        baseline=baseline,
        selection=selection,
    )
    return RepositorySnapshotResult(snapshot=RepositorySnapshot(
        facts=facts,
        baseline=baseline,
    ))


def baseline_manifest_to_dict(manifest: BaselineManifest) -> dict[str, object]:
    return {
        "schema": manifest.schema,
        "sha256": manifest.sha256,
        "entries": [
            {
                "path": entry.path,
                "sha256": entry.sha256,
                "size": entry.size,
                **({"mode": entry.mode} if entry.mode is not None else {}),
            }
            for entry in manifest.entries
        ],
    }


def repository_facts_to_dict(facts: RepositoryFacts) -> dict[str, object]:
    return {
        "schema": facts.schema,
        "sha256": facts.sha256,
        "repository_available": facts.repository_available,
        "git_repository": facts.git_repository,
        "base_commit": facts.base_commit,
        "dirty": facts.dirty,
        "top_level_entries": list(facts.top_level_entries),
        "manifests": list(facts.manifests),
        "detected_entrypoints": list(facts.detected_entrypoints),
        "file_count": facts.file_count,
        "total_bytes": facts.total_bytes,
        "source_selection": (
            source_selection_to_dict(facts.source_selection)
            if facts.source_selection is not None
            else None
        ),
    }


def canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def repository_identity(root: Path | None) -> str | None:
    """Canonical identity of a repository for the serial integration lane.

    Identity follows the repository, never the project name, so alternate
    configured paths to the same repository resolve together. For Git projects
    the resolved ``git rev-parse --show-toplevel`` directory is canonicalized so
    a subdirectory path and its top-level path map to one identity; otherwise
    the resolved real path is used (symlinks resolve identically). ``None`` is
    returned when no repository is configured.
    """
    if root is None:
        return None
    resolved = _resolve_repository_root(root)
    payload = {
        "schema": REPOSITORY_IDENTITY_SCHEMA,
        "path": str(resolved),
    }
    return canonical_sha256(payload)


def _resolve_repository_root(root: Path) -> Path:
    resolved = root.resolve()
    toplevel = _run_git(resolved, "rev-parse", "--show-toplevel")
    if toplevel is not None:
        return Path(toplevel.strip()).resolve()
    return resolved


def _repository_files(root: Path, selection: SourceSelection | None = None) -> Iterator[Path]:
    """Legacy public alias for the single deliverable walk with an optional rule."""
    if selection is None:
        selection = discover_source_selection(root)
    yield from iter_deliverable_files(root, selection)


def iter_deliverable_files(
    root: Path,
    selection: SourceSelection | None = None,
) -> Iterator[Path]:
    """Yield the deterministic deliverable source surface for ``root``.

    This is the ONE source-selection rule applied everywhere: baseline,
    workspace, sealed candidate, verifier, and target. When ``selection`` is
    ``None`` the rule is discovered from ``root`` (live repositories discover the
    full Git rule; any other tree uses the documented non-Git rule). A caller
    that already has frozen rule inputs passes them so a workspace without
    ``.git`` applies the identical selection.
    """
    root = root.resolve()
    if selection is None:
        selection = discover_source_selection(root)
    if selection.git:
        yield from _walk_git_deliverables(root, selection)
    else:
        yield from _walk_non_git_deliverables(root, selection)


def discover_source_selection(root: Path) -> SourceSelection:
    """Discover the complete source-selection rule for a live source root."""
    root = root.resolve()
    if _is_git_repository(root):
        tracked = _git_tracked_paths(root)
        ignore_files = _read_ignore_files(root)
        return SourceSelection(
            schema=SOURCE_SELECTION_SCHEMA,
            mode="git",
            version=SOURCE_SELECTION_VERSION,
            tracked_paths=tuple(tracked),
            ignore_files=ignore_files,
        )
    return SourceSelection(
        schema=SOURCE_SELECTION_SCHEMA,
        mode="non_git",
        version=SOURCE_SELECTION_VERSION,
    )


def _walk_non_git_deliverables(root: Path, selection: SourceSelection) -> Iterator[Path]:
    def scan_failed(error: OSError) -> None:
        raise error

    for current, directory_names, file_names in os.walk(root, onerror=scan_failed):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not _reserved_or_cache_directory_name(name)
        )
        current_path = Path(current)
        for directory in directory_names:
            if (current_path / directory).is_symlink():
                raise OSError(f"Source symlink requires explicit snapshot support: {current_path / directory}")
        for file_name in sorted(file_names):
            path = current_path / file_name
            if path.is_symlink():
                raise OSError(f"Source symlink requires explicit snapshot support: {path}")
            if not path.is_file():
                raise OSError(f"Unsupported source filesystem entry (expected a regular file): {path}")
            relative = path.relative_to(root).as_posix()
            if _reserved_or_cache_path(selection, relative):
                continue
            yield path


def _walk_git_deliverables(root: Path, selection: SourceSelection) -> Iterator[Path]:
    def scan_failed(error: OSError) -> None:
        raise error

    tracked = set(selection.tracked_paths)
    for current, directory_names, file_names in os.walk(root, onerror=scan_failed):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(directory_names):
            directory = current_path / name
            relative_dir = _relative_posix(current, directory, root)
            if _safe_prune_git_directory(selection, tracked, relative_dir):
                continue
            if directory.is_symlink():
                raise OSError(f"Source symlink requires explicit snapshot support: {directory}")
            kept.append(name)
        directory_names[:] = sorted(kept)
        for file_name in sorted(file_names):
            path = current_path / file_name
            if path.is_symlink():
                raise OSError(f"Source symlink requires explicit snapshot support: {path}")
            if not path.is_file():
                raise OSError(f"Unsupported source filesystem entry (expected a regular file): {path}")
            relative = _relative_posix(current, path, root)
            if _is_reserved_path(relative):
                # Reserved namespaces are never application delivery.
                continue
            if relative in tracked:
                # Tracked regular files are retained even under cache-like names.
                yield path
                continue
            if _ignored_unknown_by_rule(selection, relative, is_dir=False):
                # Exclude untracked declared caches and Git-ignored new material.
                continue
            yield path


def _safe_prune_git_directory(
    selection: SourceSelection,
    tracked: set[str],
    relative_dir: str,
) -> bool:
    # Reserved namespaces are always pruned (never walked or delivered).
    if _is_reserved_path(relative_dir + "/"):
        return True
    # A Git-ignored OR declared-cache directory with no tracked regular file
    # beneath it can be pruned without descending.
    if _ignored_unknown_by_rule(selection, relative_dir, is_dir=True) and not _any_tracked_under(tracked, relative_dir):
        return True
    return False


def _any_tracked_under(tracked: set[str], directory: str) -> bool:
    prefix = directory + "/"
    for path in tracked:
        if path == directory or path.startswith(prefix):
            return True
    return False


def _relative_posix(base: Path, target: Path, root: Path) -> str:
    return target.relative_to(root).as_posix()


def _reserved_or_cache_directory_name(name: str) -> bool:
    return name in RESERVED_DIRECTORY_NAMES or name in DECLARED_CACHE_DIRECTORY_NAMES


def _is_reserved_path(relative: str) -> bool:
    parts = relative.rstrip("/").split("/")
    return any(part in RESERVED_DIRECTORY_NAMES for part in parts)


def _reserved_or_cache_path(selection: SourceSelection, relative: str) -> bool:
    return _is_reserved_path(relative) or _under_declared_cache(relative)


def _under_declared_cache(relative: str) -> bool:
    parts = relative.rstrip("/").split("/")
    return any(part in DECLARED_CACHE_DIRECTORY_NAMES for part in parts)


def _reserved_namespace_violation(root: Path, selection: SourceSelection) -> str | None:
    # The reserved ``.open-tulid`` namespace is never application delivery. A
    # repository is diagnosed when it attempts to USE that namespace — i.e. it
    # Git-tracks content under it. A transient ``.open-tulid`` folder dropped by
    # a tool without tracking is never delivered and is not a misuse.
    if selection.git:
        for path in selection.tracked_paths:
            if path == ".open-tulid" or path.startswith(".open-tulid/"):
                return (
                    f"Repository tracks source under Tulid's reserved .open-tulid "
                    f"namespace: {path}. That namespace is never application delivery."
                )
    return None


def _is_git_repository(root: Path) -> bool:
    # A workspace copy never carries ``.git``; short-circuit on the directory so
    # we do not spawn git for trees that cannot be a Git repository.
    if not (root / ".git").exists():
        return False
    result = _run_git_verbose(root, "rev-parse", "--is-inside-work-tree")
    return result == "true\n" or result == "true"


def _git_tracked_paths(root: Path) -> set[str]:
    result = _run_git_verbose(root, "ls-files", "-z")
    if result is None:
        return set()
    return {path for path in result.split("\0") if path}


def _run_git_verbose(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _read_ignore_files(root: Path) -> tuple[IgnoreFile, ...]:
    """Read every ``.gitignore`` decision layer in Git discovery precedence order.

    Layers are ordered shallowest first (root first), so a deeper ``.gitignore``
    overrides a shallower one when the matcher evaluates them in order.
    """
    ignore_files: list[IgnoreFile] = []
    for raw_current, dirnames, filenames in os.walk(root, onerror=lambda _err: None):
        current = Path(raw_current)
        if ".gitignore" in filenames:
            path = current / ".gitignore"
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = ""
            base_dir = current.relative_to(root).as_posix() if current != root else ""
            ignore_files.append(IgnoreFile(
                base_dir=base_dir,
                content=content,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            ))
        dirnames[:] = [name for name in sorted(dirnames)
                       if name not in RESERVED_DIRECTORY_NAMES]
    ignore_files.sort(key=lambda f: (_depth(f.base_dir), f.base_dir))
    return tuple(ignore_files)


def _depth(base_dir: str) -> int:
    return 0 if not base_dir else base_dir.count("/") + 1


def _ignored_unknown_by_rule(selection: SourceSelection, relative: str, *, is_dir: bool) -> bool:
    """Exclude untracked declared caches and Git-ignored new material."""
    if _under_declared_cache(relative):
        return True
    if not selection.ignore_files:
        return False
    return _gitignore_matches(relative, is_dir=is_dir, ignore_files=selection.ignore_files)


def _gitignore_matches(relative: str, *, is_dir: bool, ignore_files: Sequence[IgnoreFile]) -> bool:
    """Deterministic Git ignore evaluation for a repository-relative path.

    A parent directory that is itself ignored makes the whole subtree ignored
    (Git forbids re-including a file under an excluded directory). Otherwise the
    applicable ``.gitignore`` layers are evaluated shallowest-first so a deeper
    layer overrides a shallower one, and the last matching pattern within each
    layer wins (``!`` negation re-includes).
    """
    parts = relative.rstrip("/").split("/")
    if not parts or parts == [""]:
        return False
    # Parent-directory exclusion dominates.
    for end in range(1, len(parts)):
        ancestor = "/".join(parts[:end])
        if _layer_match(ancestor, is_dir=True, ignore_files=ignore_files):
            return True
    return _layer_match(relative, is_dir=is_dir, ignore_files=ignore_files)


def _layer_match(relative: str, *, is_dir: bool, ignore_files: Sequence[IgnoreFile]) -> bool:
    matched = False
    for ignore_file in ignore_files:
        if not _under_dir(relative, ignore_file.base_dir):
            continue
        rel_to_base = _rel_to_base(relative, ignore_file.base_dir)
        for line in ignore_file.content.splitlines():
            pattern = line.strip()
            if not pattern or pattern.startswith("#"):
                continue
            if not _pattern_matches(pattern, rel_to_base, is_dir=is_dir):
                continue
            matched = not pattern.startswith("!")
    return matched


def _under_dir(relative: str, base_dir: str) -> bool:
    if not base_dir:
        return True
    return relative == base_dir or relative.startswith(base_dir + "/")


def _rel_to_base(relative: str, base_dir: str) -> str:
    if not base_dir:
        return relative
    if relative == base_dir:
        return ""
    return relative[len(base_dir) + 1:]


def _pattern_matches(pattern: str, path: str, *, is_dir: bool) -> bool:
    negated = pattern.startswith("!")
    body = pattern[1:] if negated else pattern
    directory_only = body.endswith("/")
    if directory_only:
        body = body[:-1]
    anchored = body.startswith("/")
    if anchored:
        body = body[1:]
    slash = "/" in body
    body = body.rstrip("/")

    if slash:
        target = path
        regex = _glob_regex(body, anchored=anchored)
    else:
        # A pattern with no slash matches basename at any depth; ``**/`` spreads it.
        if body.startswith("**/"):
            body = body[3:]
        target = path.rsplit("/", 1)[-1]
        regex = _glob_regex(body, anchored=False)
    matched = regex.fullmatch(target or "/") is not None
    if not matched:
        return False
    if directory_only:
        return is_dir
    return True


def _glob_regex(pattern: str, *, anchored: bool) -> re.Pattern[str]:
    # Translate git wildmatch (subset) to a regular expression. ``**`` spans path
    # separators; ``*`` and ``?`` do not cross a separator; ``[...]`` is a class.
    pieces: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 2] == "**":
                pieces.append(".*")
                i += 2
            else:
                pieces.append("[^/]*")
                i += 1
        elif char == "?":
            pieces.append("[^/]")
            i += 1
        elif char == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                pieces.append("\\[")
                i += 1
            else:
                char_class = pattern[i + 1:j]
                pieces.append("[" + char_class + "]")
                i = j + 1
        else:
            pieces.append(re.escape(char))
            i += 1
    regex = "".join(pieces)
    if anchored:
        regex = "^" + regex
    else:
        regex = "^(?:.*/)?" + regex
    return re.compile(regex)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _baseline_manifest(entries: tuple[FileManifestEntry, ...]) -> BaselineManifest:
    ordered = tuple(sorted(entries, key=lambda entry: entry.path))
    payload = {
        "schema": BASELINE_MANIFEST_SCHEMA,
        "entries": [
            {
                "path": entry.path,
                "sha256": entry.sha256,
                "size": entry.size,
                **({"mode": entry.mode} if entry.mode is not None else {}),
            }
            for entry in ordered
        ],
    }
    return BaselineManifest(
        schema=BASELINE_MANIFEST_SCHEMA,
        entries=ordered,
        sha256=canonical_sha256(payload),
    )


def _repository_facts(
    *,
    repository_available: bool,
    git_repository: bool,
    base_commit: str | None,
    dirty: bool | None,
    top_level_entries: tuple[str, ...],
    manifests: tuple[str, ...],
    detected_entrypoints: tuple[str, ...],
    baseline: BaselineManifest,
    selection: SourceSelection | None = None,
) -> RepositoryFacts:
    payload = {
        "schema": REPOSITORY_FACTS_SCHEMA,
        "repository_available": repository_available,
        "git_repository": git_repository,
        "base_commit": base_commit,
        "dirty": dirty,
        "top_level_entries": list(top_level_entries),
        "manifests": list(manifests),
        "detected_entrypoints": list(detected_entrypoints),
        "file_count": len(baseline.entries),
        "total_bytes": sum(entry.size for entry in baseline.entries),
        "baseline_manifest_sha256": baseline.sha256,
        "source_selection": (
            source_selection_to_dict(selection) if selection is not None else None
        ),
    }
    return RepositoryFacts(
        schema=REPOSITORY_FACTS_SCHEMA,
        repository_available=repository_available,
        git_repository=git_repository,
        base_commit=base_commit,
        dirty=dirty,
        top_level_entries=top_level_entries,
        manifests=manifests,
        detected_entrypoints=detected_entrypoints,
        file_count=len(baseline.entries),
        total_bytes=sum(entry.size for entry in baseline.entries),
        sha256=canonical_sha256(payload),
        source_selection=selection,
    )


def _git_facts(root: Path) -> tuple[bool, str | None, bool | None]:
    if not (root / ".git").exists():
        return False, None, None
    commit = _run_git(root, "rev-parse", "--verify", "HEAD")
    if commit is None:
        return False, None, None
    status = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    return True, commit.strip(), None if status is None else bool(status.strip())


def _run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _detect_entrypoints(
    root: Path,
    entries: tuple[FileManifestEntry, ...],
) -> tuple[str, ...]:
    paths = {entry.path for entry in entries}
    detected = {
        path
        for path in paths
        if Path(path).name in KNOWN_ENTRYPOINT_FILES
    }
    package_path = root / "package.json"
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            package = {}
        scripts = package.get("scripts") if isinstance(package, Mapping) else None
        if isinstance(scripts, Mapping):
            detected.update(
                f"package.json#scripts.{name}"
                for name in sorted(str(key) for key in scripts)
            )

    pyproject_path = root / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            pyproject = {}
        project = pyproject.get("project") if isinstance(pyproject, Mapping) else None
        scripts = project.get("scripts") if isinstance(project, Mapping) else None
        if isinstance(scripts, Mapping):
            detected.update(
                f"pyproject.toml#project.scripts.{name}"
                for name in sorted(str(key) for key in scripts)
            )
        if isinstance(pyproject, Mapping) and "build-system" in pyproject:
            detected.add("pyproject.toml#build-system")
        tool = pyproject.get("tool") if isinstance(pyproject, Mapping) else None
        if isinstance(tool, Mapping) and "pytest" in tool:
            detected.add("pyproject.toml#tool.pytest")

    makefile = root / "Makefile"
    if makefile.is_file():
        try:
            for line in makefile.read_text(encoding="utf-8").splitlines():
                match = MAKE_TARGET_RE.match(line)
                if match and not match.group(1).startswith("."):
                    detected.add(f"Makefile#{match.group(1)}")
        except (OSError, UnicodeDecodeError):
            pass
    return tuple(sorted(detected))
