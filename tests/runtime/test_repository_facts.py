from __future__ import annotations

import os

import pytest

from open_tulid.runtime.repository_facts import capture_repository_snapshot, repository_identity


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
