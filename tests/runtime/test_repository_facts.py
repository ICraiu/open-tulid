from __future__ import annotations

from open_tulid.runtime.repository_facts import capture_repository_snapshot


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
