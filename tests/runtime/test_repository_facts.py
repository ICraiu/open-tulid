from __future__ import annotations

import os
import subprocess

import pytest

from open_tulid.runtime.repository_facts import (
    capture_repository_snapshot,
    discover_source_selection,
    iter_deliverable_files,
    repository_identity,
)


def _git(root, *args):
    subprocess.run(("git", "-C", str(root), *args), check=False, capture_output=True)


def _git_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "tester")
    return repo


def _commit_all(repo):
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")


def test_repository_identity_resolves_alternate_paths_to_one_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    alias = tmp_path / "alias"
    try:
        os.symlink(repo, alias)
    except OSError:
        pytest.skip("symlinks unavailable")

    root_identity = repository_identity(repo)
    alias_identity = repository_identity(alias)
    other = tmp_path / "other"
    other.mkdir()

    assert root_identity is not None
    assert root_identity == alias_identity
    assert root_identity != repository_identity(other)


def test_repository_identity_is_none_without_a_repository():
    assert repository_identity(None) is None


def test_git_repository_identity_uses_top_level_directory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subdir = repo / "src"
    subdir.mkdir()
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email test@example.com")
    os.system(f"git -C {repo} config user.name tester")

    assert repository_identity(repo) == repository_identity(subdir)


def test_repository_snapshot_captures_deterministic_facts_and_baseline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("pyproject.toml").write_text(
        """\
[build-system]
requires = ["setuptools"]

[project]
name = "demo"

[project.scripts]
demo = "demo:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
        encoding="utf-8",
    )
    repo.joinpath("Makefile").write_text(
        "test:\n\tpython -m pytest\n\nbuild:\n\tpython -m build\n",
        encoding="utf-8",
    )
    repo.joinpath("src").mkdir()
    repo.joinpath("src", "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    repo.joinpath(".pytest_cache").mkdir()
    repo.joinpath(".pytest_cache", "ignored").write_text("noise\n", encoding="utf-8")

    first = capture_repository_snapshot(repo)
    second = capture_repository_snapshot(repo)

    assert first.accepted is True
    assert first.snapshot is not None
    assert second.snapshot == first.snapshot
    assert first.snapshot.facts.manifests == ("pyproject.toml",)
    assert first.snapshot.facts.detected_entrypoints == (
        "Makefile",
        "Makefile#build",
        "Makefile#test",
        "pyproject.toml#build-system",
        "pyproject.toml#project.scripts.demo",
        "pyproject.toml#tool.pytest",
    )
    assert [entry.path for entry in first.snapshot.baseline.entries] == [
        "Makefile",
        "pyproject.toml",
        "src/demo.py",
    ]
    assert first.snapshot.facts.file_count == 3


def test_repository_baseline_changes_when_file_content_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")

    before = capture_repository_snapshot(repo)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    after = capture_repository_snapshot(repo)

    assert before.snapshot is not None
    assert after.snapshot is not None
    assert before.snapshot.baseline.sha256 != after.snapshot.baseline.sha256
    assert before.snapshot.facts.sha256 != after.snapshot.facts.sha256


def test_missing_repository_is_reported(tmp_path):
    result = capture_repository_snapshot(tmp_path / "missing")

    assert result.accepted is False
    assert result.errors[0].code == "repository.missing"


def test_absent_repository_produces_an_explicit_empty_snapshot():
    result = capture_repository_snapshot(None)

    assert result.accepted is True
    assert result.snapshot is not None
    assert result.snapshot.facts.repository_available is False
    assert result.snapshot.baseline.entries == ()


def test_git_rule_retains_tracked_under_cache_and_excludes_untracked(tmp_path):
    repo = _git_repo(tmp_path)
    owned = repo / "node_modules" / "project-owned"
    owned.mkdir(parents=True)
    (owned / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    _commit_all(repo)
    # An untracked dependency the worker installs beside the tracked file.
    (owned / "dep.js").write_text("vendor noise\n", encoding="utf-8")

    snapshot = capture_repository_snapshot(repo)
    assert snapshot.accepted is True
    assert snapshot.snapshot is not None
    paths = {entry.path for entry in snapshot.snapshot.baseline.entries}

    # Tracked regular files under cache-like names are retained (no name-only pruning).
    assert "node_modules/project-owned/index.js" in paths
    # Untracked declared cache content is excluded.
    assert "node_modules/project-owned/dep.js" not in paths


@pytest.mark.parametrize("directory", ["dist", "build", "output", "target"])
def test_git_rule_retains_tracked_source_under_output_names_and_hidden_dirs(tmp_path, directory):
    repo = _git_repo(tmp_path)
    target = repo / directory
    target.mkdir()
    (target / "source.ts").write_text(f"{directory}-source\n", encoding="utf-8")
    hidden = repo / ".ci"
    hidden.mkdir()
    (hidden / "config.sh").write_text("set -e\n", encoding="utf-8")
    _commit_all(repo)

    snapshot = capture_repository_snapshot(repo)
    assert snapshot.accepted is True
    paths = {entry.path for entry in snapshot.snapshot.baseline.entries}
    assert f"{directory}/source.ts" in paths
    assert ".ci/config.sh" in paths


def test_git_rule_adds_permitted_new_source_and_excludes_ignored_secret(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n.env\n", encoding="utf-8")
    _commit_all(repo)
    # A non-ignored new source file and an ignored secret created after commit.
    (repo / "new_feature.py").write_text("def feature():\n    pass\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")

    snapshot = capture_repository_snapshot(repo)
    assert snapshot.accepted is True
    paths = {entry.path for entry in snapshot.snapshot.baseline.entries}
    assert "new_feature.py" in paths
    assert ".env" not in paths


def test_gitignore_edit_reclassifies_new_material_within_candidate(tmp_path):
    from open_tulid.runtime.workspaces import _copy_repo
    from open_tulid.runtime.candidate import capture_candidate

    repo = _git_repo(tmp_path)
    (repo / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _commit_all(repo)
    selection = discover_source_selection(repo)
    baseline = capture_repository_snapshot(repo).snapshot.baseline

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _copy_repo(repo, workspace, selection)
    # The worker edits .gitignore and adds a file the edited rule now ignores.
    (workspace / ".gitignore").write_text("*.log\n*.txt\n", encoding="utf-8")
    (workspace / "secret.txt").write_text("SENSITIVE\n", encoding="utf-8")

    result = capture_candidate(
        workspace=workspace,
        storage_root=tmp_path / "cands",
        candidate_id="row5",
        baseline=baseline,
        selection=selection,
    )
    assert result.accepted is True
    assert result.captured is not None
    kinds = {change.path: change.kind for change in result.captured.candidate.changes}
    # Deterministic re-classification: the document .gitignore edit travels, the
    # newly added file covered by the candidate's rule is excluded.
    assert "secret.txt" not in kinds
    assert kinds.get(".gitignore") == "edit"
    # The candidate identity binds the edited rule inputs.
    assert result.captured.candidate.source_selection_sha256 is not None


def test_tracked_deletion_remains_authoritative_even_if_ignored(tmp_path):
    from open_tulid.runtime.workspaces import _copy_repo
    from open_tulid.runtime.candidate import KIND_DELETE, capture_candidate

    repo = _git_repo(tmp_path)
    (repo / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit_all(repo)
    # Track the file, then make an ignore rule cover it. Gitignore never detaches
    # a tracked file, so its deletion must remain an authoritative change.
    (repo / ".gitignore").write_text("kept.py\n", encoding="utf-8")
    selection = discover_source_selection(repo)
    baseline = capture_repository_snapshot(repo).snapshot.baseline
    assert "kept.py" in {entry.path for entry in baseline.entries}

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _copy_repo(repo, workspace, selection)
    (workspace / "kept.py").unlink()

    result = capture_candidate(
        workspace=workspace,
        storage_root=tmp_path / "cands",
        candidate_id="row6",
        baseline=baseline,
        selection=selection,
    )
    assert result.accepted is True
    assert result.captured is not None
    assert any(change.path == "kept.py" and change.kind == KIND_DELETE
               for change in result.captured.candidate.changes)


def test_reserved_namespace_is_diagnosed_when_tracked(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "app.py").write_text("print('app')\n", encoding="utf-8")
    (repo / ".open-tulid").mkdir()
    (repo / ".open-tulid" / "leaked.md").write_text("internal", encoding="utf-8")
    _commit_all(repo)

    snapshot = capture_repository_snapshot(repo)
    assert snapshot.accepted is False
    assert snapshot.errors[0].code == "repository.reserved_namespace"
    assert ".open-tulid" in snapshot.errors[0].message


def test_tracked_under_cache_is_in_deliverable_walk_and_untracked_cache_is_not(tmp_path):
    # Negative proof: a name-only walk that prunes node_modules would drop the
    # tracked source; the one source-selection rule must retain it.
    repo = _git_repo(tmp_path)
    owned = repo / "node_modules" / "project-owned"
    owned.mkdir(parents=True)
    (owned / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    _commit_all(repo)
    (owned / "dep.js").write_text("vendor noise\n", encoding="utf-8")

    selection = discover_source_selection(repo)
    walked = {path.relative_to(repo).as_posix() for path in iter_deliverable_files(repo, selection)}
    assert "node_modules/project-owned/index.js" in walked
    assert "node_modules/project-owned/dep.js" not in walked
