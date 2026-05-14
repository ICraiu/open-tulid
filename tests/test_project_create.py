from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_tulid.cli.main import app
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME, load_config
from open_tulid.models import Config
from open_tulid.vault.project import create_project, iter_configured_projects

runner = CliRunner()


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config_file(tmp_vault: Path) -> Path:
    config_dir = tmp_vault / CONFIG_DIRNAME
    config_dir.mkdir()
    cfg = config_dir / CONFIG_FILENAME
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
        assert (tmp_vault / "Engine" / "events").is_dir()
        assert "Engine/kanban" in result.created_dirs
        assert "Engine/docs" in result.created_dirs
        assert "Engine/tasks" in result.created_dirs
        assert "Engine/events" in result.created_dirs

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
            assert (tmp_vault / "Engine" / "events").is_dir()
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

    def test_project_cli_validates_configured_workflow(self, tmp_vault: Path, config_file: Path):
        (config_file.parent / "workflow.yaml").write_text(
            "schema_version: 1\nstatements:\n  - kind: state_type\n    id: Todo\n",
            encoding="utf-8",
        )
        with config_file.open("a", encoding="utf-8") as f:
            f.write('\n[workflow]\npath = "workflow.yaml"\n')
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            result = runner.invoke(app, ["project", "Engine"])
            assert result.exit_code == 2
            assert "Workflow validation failed." in result.output
        finally:
            os.chdir(original)


class TestInitCommand:
    def test_init_creates_tuluid_config_and_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))

        result = runner.invoke(app, ["init"])

        assert result.exit_code == 0
        config_dir = tmp_path / CONFIG_DIRNAME
        config_path = config_dir / CONFIG_FILENAME
        workflow_path = config_dir / "workflow.yaml"
        assert config_path.is_file()
        assert workflow_path.is_file()
        assert "[workflow]" in config_path.read_text(encoding="utf-8")
        assert "[runtime]" in config_path.read_text(encoding="utf-8")
        assert "schema_version: 1" in workflow_path.read_text(encoding="utf-8")

    def test_init_refuses_existing_workflow_without_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        config_dir = tmp_path / CONFIG_DIRNAME
        config_dir.mkdir()
        (config_dir / "workflow.yaml").write_text("existing", encoding="utf-8")

        result = runner.invoke(app, ["init"])

        assert result.exit_code == 1
        assert "Workflow already exists" in result.output


class TestConfigLoading:
    def test_config_loaded_from_tuluid_directory(self, tmp_vault: Path):
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        workflow = config_dir / "workflow.yaml"
        workflow.write_text(
            "schema_version: 1\nstatements:\n  - kind: state\n    id: Todo\n",
            encoding="utf-8",
        )
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["TestProject"]\n'
            '\n[workflow]\npath = "workflow.yaml"\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            config = load_config()
            assert config.config_dir == config_dir
            assert config.workflow_path == workflow
        finally:
            os.chdir(original)

    def test_config_loads_per_project_coding_settings(self, tmp_vault: Path):
        repo_root = tmp_vault / "repos" / "open-tulid"
        repo_root.mkdir(parents=True)
        (tmp_vault / "Tracker").mkdir()
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["Agent"]\n'
            '\n[projects.Agent]\n'
            'tracker_path = "Tracker"\n'
            f'repo_root = "{repo_root}"\n'
            'main_branch = "trunk"\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            config = load_config()
            project = config.project_configs["Agent"]
            assert project.tracker_path == "Tracker"
            assert project.repo_root == repo_root
            assert project.main_branch == "trunk"
            configured = iter_configured_projects(config)[0]
            assert configured.name == "Agent"
            assert configured.path == tmp_vault / "Tracker"
            assert configured.repo_root == repo_root
            assert configured.main_branch == "trunk"
        finally:
            os.chdir(original)

    def test_config_defaults_project_coding_settings(self, tmp_vault: Path):
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["Agent"]\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            config = load_config()
            project = config.project_configs["Agent"]
            assert project.tracker_path == "Agent"
            assert project.repo_root is None
            assert project.main_branch == "main"
        finally:
            os.chdir(original)

    def test_config_loads_runtime_settings(self, tmp_vault: Path):
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        workspaces = tmp_vault / "workspaces"
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["Agent"]\n'
            '\n[runtime]\n'
            'docker_executable = "podman"\n'
            'container_workspace = "/workspace/custom"\n'
            'image_tag_prefix = "registry.local/tulid/agent"\n'
            'default_timeout_seconds = 45\n'
            'shared_workspace_root = "../workspaces"\n'
            'completion_host = "127.0.0.1"\n'
            'completion_port = 8765\n'
            'completion_container_host = "host.test"\n'
            'container_volume_relabel = true\n'
            '\n[runtime.worker_images]\n'
            'codex = "registry.local/codex:dev"\n'
            '\n[runtime.worker_args]\n'
            'codex = ["exec", "{prompt_packet}"]\n'
            '\n[runtime.env]\n'
            'OPEN_TULID_ENV = "test"\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            config = load_config()
            assert config.runtime.docker_executable == "podman"
            assert config.runtime.container_workspace == "/workspace/custom"
            assert config.runtime.image_tag_prefix == "registry.local/tulid/agent"
            assert config.runtime.default_timeout_seconds == 45
            assert config.runtime.shared_workspace_root == workspaces
            assert config.runtime.worker_images == {"codex": "registry.local/codex:dev"}
            assert config.runtime.worker_args == {"codex": ("exec", "{prompt_packet}")}
            assert config.runtime.completion_host == "127.0.0.1"
            assert config.runtime.completion_port == 8765
            assert config.runtime.completion_container_host == "host.test"
            assert config.runtime.container_volume_relabel is True
            assert config.runtime.env == {"OPEN_TULID_ENV": "test"}
        finally:
            os.chdir(original)

    def test_config_rejects_secret_like_runtime_env(self, tmp_vault: Path):
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["Agent"]\n'
            '\n[runtime.env]\n'
            'OPENAI_API_KEY = "secret"\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_rejects_relative_container_workspace(self, tmp_vault: Path):
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["Agent"]\n'
            '\n[runtime]\n'
            'container_workspace = "workspace/project"\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit):
                load_config()
        finally:
            os.chdir(original)

    def test_config_rejects_missing_repo_root_directory(self, tmp_vault: Path):
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["Agent"]\n'
            '\n[projects.Agent]\n'
            f'repo_root = "{tmp_vault / "missing"}"\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_rejects_tracker_path_escape(self, tmp_vault: Path):
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["Agent"]\n'
            '\n[projects.Agent]\n'
            'tracker_path = "../Agent"\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_legacy_home_config_is_loaded(self, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch):
        home = tmp_vault / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        cfg = home / ".open-tulid.toml"
        cfg.write_text(
            f'[vault]\nroot = "{tmp_vault}"\nprojects = ["TestProject"]\n',
            encoding="utf-8",
        )
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            config = load_config()
            assert config.vault_root == tmp_vault
        finally:
            os.chdir(original)

    def test_config_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_missing_vault_section(self, tmp_path: Path):
        config_dir = tmp_path / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text('[other]\nfoo = "bar"\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_vault_section_must_be_table(self, tmp_path: Path):
        config_dir = tmp_path / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text('vault = "nope"\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_missing_vault_root(self, tmp_path: Path):
        config_dir = tmp_path / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text('[vault]\nprojects = ["Test"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_vault_root_must_be_string(self, tmp_path: Path):
        config_dir = tmp_path / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text('[vault]\nroot = 123\nprojects = ["Test"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)

    def test_config_vault_root_not_exists(self, tmp_path: Path):
        config_dir = tmp_path / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
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
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
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
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
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
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
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
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
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
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
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
        config_dir = tmp_vault / CONFIG_DIRNAME
        config_dir.mkdir()
        cfg = config_dir / CONFIG_FILENAME
        cfg.write_text(f'[vault]\nroot = "{tmp_vault}"\nprojects = ["/etc"]\n', encoding="utf-8")
        original = os.getcwd()
        try:
            os.chdir(tmp_vault)
            with pytest.raises(SystemExit) as exc_info:
                load_config()
            assert exc_info.value.code == 2
        finally:
            os.chdir(original)
