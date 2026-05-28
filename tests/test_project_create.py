from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_tulid.adapters import default_adapter_type
from open_tulid.cli.main import app
from open_tulid.config import CONFIG_DIRNAME, CONFIG_FILENAME, load_config
from open_tulid.models import Config, ProjectConfig
from open_tulid.vault.project import create_project, iter_configured_projects
from open_tulid.workflow.runtime import load_workflow_definition

runner = CliRunner()


def _write_config(home: Path, tracker_root: Path, body: str | None = None) -> Path:
    config_dir = home / CONFIG_DIRNAME
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg = config_dir / CONFIG_FILENAME
    tracker_type = default_adapter_type()
    cfg.write_text(body or (
        f"tracker:\n  type: {tracker_type}\n  root: {tracker_root}\n"
        "projects:\n  Engine: {}\n"
    ), encoding="utf-8")
    return cfg


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    return tmp_path / "tracker"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def config_file(tmp_path: Path, tmp_vault: Path) -> Path:
    tmp_vault.mkdir()
    return _write_config(tmp_path, tmp_vault)


@pytest.fixture
def valid_config(tmp_vault: Path) -> Config:
    tmp_vault.mkdir()
    return Config(
        vault_root=tmp_vault,
        projects=["Engine"],
        project_configs={"Engine": ProjectConfig(name="Engine", tracker_path="Engine", workflow_path=tmp_vault / "Engine" / "workflow.yaml")},
    )


class TestProjectCreation:
    def test_project_creates_project_owned_scaffold(self, tmp_vault: Path, valid_config: Config):
        result = create_project(valid_config, "Engine")
        assert result.name == "Engine"
        for dirname in ("kanban", "docs", "tasks", "events", "agents"):
            assert (tmp_vault / "Engine" / dirname).is_dir()
            assert f"Engine/{dirname}" in result.created_dirs
        assert (tmp_vault / "Engine" / "workflow.yaml").is_file()
        workflow_text = (tmp_vault / "Engine" / "workflow.yaml").read_text(encoding="utf-8")
        assert "DraftDirection" in workflow_text
        assert "STT" not in workflow_text
        loaded_workflow = load_workflow_definition(tmp_vault / "Engine" / "workflow.yaml")
        assert loaded_workflow.valid is True
        assert loaded_workflow.definition is not None
        assert "ProductIdea" in loaded_workflow.definition.task_types
        assert (tmp_vault / "Engine" / "kanban" / "Work.md").is_file()
        assert "## Idea" in (tmp_vault / "Engine" / "kanban" / "Work.md").read_text(encoding="utf-8")
        assert (tmp_vault / "Engine" / "Docker.tulid").is_file()
        assert "FROM ${TULID_AGENT_IMAGE}" in (tmp_vault / "Engine" / "Docker.tulid").read_text(encoding="utf-8")
        assert (tmp_vault / "Engine" / "agents" / "default.agent.md").is_file()
        agent_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((tmp_vault / "Engine" / "agents").glob("*.md"))
        )
        assert "STT" not in agent_text
        assert "repository files" in agent_text

    def test_project_fails_when_exists(self, valid_config: Config):
        create_project(valid_config, "Engine")
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, "Engine")
        assert exc_info.value.code == 2

    @pytest.mark.parametrize("name", ["", "Project/Subproject", "Project\\Sub", "../Engine", "/tmp/Engine"])
    def test_project_rejects_invalid_names(self, valid_config: Config, name: str):
        with pytest.raises(SystemExit) as exc_info:
            create_project(valid_config, name)
        assert exc_info.value.code == 2

    def test_project_adds_unconfigured_project_to_in_memory_config(self, tmp_vault: Path, valid_config: Config):
        result = create_project(valid_config, "NewProject")
        assert result.name == "NewProject"
        assert valid_config.projects == ["Engine", "NewProject"]
        assert valid_config.project_configs["NewProject"].tracker_path == "NewProject"
        assert (tmp_vault / "NewProject" / "workflow.yaml").is_file()

    def test_project_cli_creates_structure(self, tmp_vault: Path, config_file: Path):
        result = runner.invoke(app, ["project", "Engine"])
        assert result.exit_code == 0
        assert "Project created: Engine" in result.output
        assert (tmp_vault / "Engine" / "workflow.yaml").is_file()

    def test_project_cli_tracks_new_project_in_config(self, tmp_vault: Path, config_file: Path):
        result = runner.invoke(app, ["project", "NewProject"])
        assert result.exit_code == 0
        assert "Project created: NewProject" in result.output
        assert (tmp_vault / "NewProject" / "workflow.yaml").is_file()
        reloaded = load_config()
        assert reloaded.projects == ["Engine", "NewProject"]
        assert reloaded.project_configs["NewProject"].tracker_path == "NewProject"


class TestInitCommand:
    def test_init_creates_home_yaml_config_only(self, tmp_path: Path):
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        config_path = tmp_path / CONFIG_DIRNAME / CONFIG_FILENAME
        assert config_path.is_file()
        text = config_path.read_text(encoding="utf-8")
        assert "tracker:" in text
        assert "projects:" in text
        assert "runtime:" in text
        assert "# Tulid stores tracker projects" in text
        assert "projects: {}" in text
        assert "failed_job_backoff_seconds: 60" in text
        assert "max_failed_attempts_per_transition: 0" in text
        assert not (config_path.parent / "workflow.yaml").exists()

    def test_init_refuses_existing_config(self, tmp_path: Path):
        config_dir = tmp_path / CONFIG_DIRNAME
        config_dir.mkdir()
        (config_dir / CONFIG_FILENAME).write_text("existing", encoding="utf-8")
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
        assert "Config already exists" in result.output


class TestConfigLoading:
    def test_config_loaded_only_from_home_yaml(self, tmp_path: Path, tmp_vault: Path):
        tmp_vault.mkdir()
        cfg = _write_config(tmp_path, tmp_vault)
        config = load_config()
        assert config.config_dir == cfg.parent
        assert config.tracker_root == tmp_vault
        assert config.project_configs["Engine"].workflow_path == tmp_vault / "Engine" / "workflow.yaml"

    def test_config_loads_per_project_settings(self, tmp_path: Path, tmp_vault: Path):
        tmp_vault.mkdir()
        repo_root = tmp_path / "repos" / "open-tulid"
        repo_root.mkdir(parents=True)
        _write_config(tmp_path, tmp_vault, (
            f"tracker:\n  type: {default_adapter_type()}\n  root: {tmp_vault}\n"
            "projects:\n  Agent:\n    path: Tracker\n"
            f"    repo_root: {repo_root}\n    main_branch: trunk\n"
        ))
        config = load_config()
        project = config.project_configs["Agent"]
        assert project.tracker_path == "Tracker"
        assert project.repo_root == repo_root
        assert project.main_branch == "trunk"
        configured = iter_configured_projects(config)[0]
        assert configured.path == tmp_vault / "Tracker"

    def test_config_loads_runtime_resources_and_proxy_settings(self, tmp_path: Path, tmp_vault: Path):
        tmp_vault.mkdir()
        _write_config(tmp_path, tmp_vault, (
            f"tracker:\n  type: {default_adapter_type()}\n  root: {tmp_vault}\nprojects:\n  Agent: {{}}\n"
            "runtime:\n  docker_executable: podman\n  container_workspace: /workspace/custom\n"
            "  image_tag_prefix: registry.local/tulid/agent\n  default_timeout_seconds: 45\n"
            "  failed_job_backoff_seconds: 5\n  max_failed_attempts_per_transition: 7\n"
            "  shared_workspace_root: ../workspaces\n  completion_host: 127.0.0.1\n  completion_port: 8765\n"
            "  completion_container_host: host.test\n  container_volume_relabel: true\n"
            "  worker_images: {codex: 'registry.local/codex:dev'}\n  worker_args: {codex: [exec, '{prompt_packet}']}\n"
            "  worker_resources: {codex: [remote-llm]}\n  worker_types: {codex: codex}\n"
            "  worker_model_env: {codex: {OPENAI_BASE_URL: '{endpoint}', OPENAI_API_KEY: '{token}'}}\n"
            "  env: {OPEN_TULID_ENV: test}\n"
            "model_proxy:\n  openai:\n    kind: openai\n    base_url: https://api.openai.com/v1\n"
            "    api_key_file: secrets/openai.key\n"
            "resources:\n  remote-llm:\n    kind: model\n    capacity: 1\n    proxy: openai\n"
        ))
        config = load_config()
        assert config.runtime.docker_executable == "podman"
        assert config.runtime.shared_workspace_root == tmp_path / "workspaces"
        assert config.runtime.failed_job_backoff_seconds == 5
        assert config.runtime.max_failed_attempts_per_transition == 7
        assert config.runtime.worker_args == {"codex": ("exec", "{prompt_packet}")}
        assert config.runtime.worker_model_env["codex"]["OPENAI_BASE_URL"] == "{endpoint}"
        assert config.model_proxy["openai"].api_key_file == tmp_path / ".tulid" / "secrets" / "openai.key"
        assert config.resources["remote-llm"].proxy == "openai"

    @pytest.mark.parametrize("body", [
        "other: {foo: bar}\n",
        f"tracker: {{type: {default_adapter_type()}, root: /tmp}}\nprojects: []\n",
    ])
    def test_config_rejects_invalid_shape(self, tmp_path: Path, body: str):
        _write_config(tmp_path, tmp_path, body)
        with pytest.raises(SystemExit) as exc_info:
            load_config()
        assert exc_info.value.code == 2

    def test_config_loads_subscription_model_backend(self, tmp_path: Path, tmp_vault: Path):
        tmp_vault.mkdir()
        _write_config(tmp_path, tmp_vault, (
            f"tracker:\n  type: {default_adapter_type()}\n  root: {tmp_vault}\nprojects:\n  Agent: {{}}\n"
            "runtime:\n  worker_resources: {codex: [codex-subscription]}\n  worker_types: {codex: codex}\n"
            "model_proxy:\n  chatgpt-codex:\n    kind: subscription\n    auth_home: ~/.codex\n"
            "resources:\n  codex-subscription:\n    kind: model\n    capacity: 4\n    proxy: chatgpt-codex\n"
        ))

        config = load_config()

        proxy = config.model_proxy["chatgpt-codex"]
        assert proxy.kind == "subscription"
        assert proxy.auth_home == tmp_path / ".codex"
        assert proxy.container_auth_home == "/root/.codex"

    def test_config_rejects_tracker_path_escape(self, tmp_path: Path, tmp_vault: Path):
        tmp_vault.mkdir()
        _write_config(tmp_path, tmp_vault, (
            f"tracker:\n  type: {default_adapter_type()}\n  root: {tmp_vault}\nprojects:\n  Agent:\n    path: ../Agent\n"
        ))
        with pytest.raises(SystemExit):
            load_config()

    def test_config_missing(self, tmp_path: Path):
        with pytest.raises(SystemExit):
            load_config()
