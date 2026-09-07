from __future__ import annotations

from pathlib import Path

from open_tulid.runtime.acceptance_profiles import load_acceptance_profiles


def test_profiles_loader_still_validates_standalone_infra(tmp_path: Path) -> None:
    # The acceptance.yaml loader remains available as separate infrastructure,
    # but it is NOT part of the command-only global contract acceptance path.
    project = tmp_path / "project"
    project.mkdir()
    project.joinpath("acceptance.yaml").write_text("""schema: tulid.acceptance/v1
policy: {require_vertical_slice: true}
profiles:
  unit:
    kind: unit
    argv: [python, check.py, unit]
""", encoding="utf-8")

    loaded = load_acceptance_profiles(project)

    assert loaded.accepted is True
    assert loaded.profiles is not None
    assert "unit" in loaded.profiles


def test_unknown_or_unsafe_profile_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project.joinpath("acceptance.yaml").write_text("""schema: tulid.acceptance/v1
profiles:
  bad:
    kind: unit
    argv: [python, check.py, '&&']
""", encoding="utf-8")

    loaded = load_acceptance_profiles(project)

    assert loaded.accepted is False
    assert loaded.errors[0].code == "acceptance_profiles.profile_invalid"


def test_missing_acceptance_file_is_not_an_error(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    loaded = load_acceptance_profiles(project)

    assert loaded.accepted is True
    assert loaded.profiles == {}
