from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_tulid.cli.main import app
from open_tulid.config import load_config
from open_tulid.models import Config
from open_tulid.vault.project import create_project

runner = CliRunner()


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config_file(tmp_vault: Path) -> Path:
    cfg = tmp_vault / ".open-tulid.toml"
    cfg.write_text(
        f'[vault]\nroot = "{tmp_vault}"\nprojects = ["TestProject"]\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def valid_config(tmp_vault: Path) -> Config:
    return Config(vault_root=tmp_vault, projects=["TestProject"])


class TestProjectCreation:
    def test_project_creates_directories(self, tmp_vault: Path, valid_config: Config):
        result = create_project(valid_config, "Engine")
        assert result.name == "Engine"
        assert (tmp_vault / "Engine" / "kanban").is_dir()
        assert (tmp_vault / "Engine" / "docs").is_dir()
        assert (tmp_vault / "Engine" / "tasks").is_dir()
        assert "Engine/kanban" in result.created_dirs
        assert "Engine/docs" in result.created_dirs
        assert "Engine/tasks" in result.created_dirs

    def test_project_fails_when_exists(self, tmp_vault: Path, valid_config: Config):
        create_project(valid_config, "Engine")
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, "Engine")
        assert exc_info.value.code == 2

    def test_project_fails_empty_name(self, tmp_vault: Path, valid_config: Config):
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, "")
        assert exc_info.value.code == 2

    def test_project_fails_name_with_slash(self, tmp_vault: Path, valid_config: Config):
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, "Project/Subproject")
        assert exc_info.value.code == 2

    def test_project_fails_name_with_backslash(self, tmp_vault: Path, valid_config: Config):
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, "Project\\Sub")
        assert exc_info.value.code == 2

    def test_project_fails_name_with_dotdot(self, tmp_vault: Path, valid_config: Config):
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, "../Engine")
        assert exc_info.value.code == 2

    def test_project_fails_absolute_name(self, tmp_vault: Path, valid_config: Config):
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, "/tmp/Engine")
        assert exc_info.value.code == 2

    def test_project_cli_creates_structure(self, tmp_vault: Path, config_file: Path):
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            result = runner.invoke(app, ["project", "Engine"])
            assert result.exit_code == 0
            assert "Project created: Engine" in result.output
            assert (tmp_vault / "Engine" / "kanban").is_dir()
            assert (tmp_vault / "Engine" / "docs").is_dir()
            assert (tmp_vault / "Engine" / "tasks").is_dir()
        finally:
            os.chdir(original)

    def test_project_cli_fails_when_exists(
        self, tmp_vault: Path, config_file: Path
    ):
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            runner.invoke(app, ["project", "Engine"])
            result = runner.invoke(app, ["project", "Engine"])
            assert result.exit_code == 2
        finally:
            os.chdir(original)

    def test_project_cli_fails_with_dotdot(
        self, tmp_vault: Path, config_file: Path
    ):
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            result = runner.invoke(app, ["project", "../Engine"])
            assert result.exit_code == 2
        finally:
            os.chdir(original)

    def test_project_cli_fails_empty_name(
        self, tmp_vault: Path, config_file: Path
    ):
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            result = runner.invoke(app, ["project", ""])
            assert result.exit_code == 2
        finally:
            os.chdir(original)


class TestConfigLoading:
    def test_config_missing(self, tmp_path: Path):
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_missing_vault_section(self, tmp_path: Path):
        cfg = tmp_path / ".open-tulid.toml"
        cfg.write_text('[other]\nfoo = "bar"\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_missing_vault_root(self, tmp_path: Path):
        cfg = tmp_path / ".open-tulid.toml"
        cfg.write_text('[vault]\nprojects = ["Test"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_vault_root_not_exists(self, tmp_path: Path):
        cfg = tmp_path / ".open-tulid.toml"
        cfg.write_text('[vault]\nroot = "/nonexistent/path"\nprojects = ["Test"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_missing_projects(self, tmp_vault: Path):
        cfg = tmp_vault / ".open-tulid.toml"
        cfg.write_text(f'[vault]\nroot = "{tmp_vault}"\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_empty_projects(self, tmp_vault: Path):
        cfg = tmp_vault / ".open-tulid.toml"
        cfg.write_text(f'[vault]\nroot = "{tmp_vault}"\nprojects = []\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_project_name_with_slash(self, tmp_vault: Path):
        cfg = tmp_vault / ".open-tulid.toml"
        cfg.write_text(f'[vault]\nroot = "{tmp_vault}"\nprojects = ["foo/bar"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_project_name_with_backslash(self, tmp_vault: Path):
        cfg = tmp_vault / ".open-tulid.toml"
        cfg.write_text(f'[vault]\nroot = "{tmp_vault}"\nprojects = ["foo\\\\bar"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_project_name_with_dotdot(self, tmp_vault: Path):
        cfg = tmp_vault / ".open-tulid.toml"
        cfg.write_text(f'[vault]\nroot = "{tmp_vault}"\nprojects = [".."]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_absolute_project_path(self, tmp_vault: Path):
        cfg = tmp_vault / ".open-tulid.toml"
        cfg.write_text(f'[vault]\nroot = "{tmp_vault}"\nprojects = ["/etc"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)
