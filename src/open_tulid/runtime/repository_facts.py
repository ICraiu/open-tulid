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
from typing import Mapping

from open_tulid.domain import DomainError


REPOSITORY_FACTS_SCHEMA = "tulid.repository-facts/v1"
BASELINE_MANIFEST_SCHEMA = "tulid.baseline-manifest/v1"

EXCLUDED_DIRECTORY_NAMES = frozenset({
    ".git",
    ".open-tulid",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "output",
    "target",
    "venv",
})

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


def capture_repository_snapshot(repo_root: Path | None) -> RepositorySnapshotResult:
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

    entries: list[FileManifestEntry] = []
    try:
        for path in _repository_files(root):
            entries.append(FileManifestEntry(
                path=path.relative_to(root).as_posix(),
                sha256=_file_sha256(path),
                size=path.stat().st_size,
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
    }


def canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _repository_files(root: Path) -> Iterator[Path]:
    for current, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in EXCLUDED_DIRECTORY_NAMES
        )
        current_path = Path(current)
        for file_name in sorted(file_names):
            path = current_path / file_name
            if path.is_file():
                yield path


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
    )


def _git_facts(root: Path) -> tuple[bool, str | None, bool | None]:
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
