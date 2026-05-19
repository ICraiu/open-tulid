from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_tulid.containers import (
    AgentRunRequest,
    DockerAvailability,
    ImageBuildResult,
    build_agent_image,
    build_agent_images,
    check_docker,
    docker_install_plan,
    get_agent_image_spec,
    list_agent_image_specs,
    run_agent_container,
)
from open_tulid.cli import main as cli_main
from open_tulid.models import RuntimeConfig
from open_tulid.containers.runtime import image_for_agent, request_for_worker


AGENT_DOCKERFILES = Path("src/open_tulid/containers/agents")
runner = CliRunner()


def test_codex_agent_dockerfile_packages_codex_cli():
    content = (AGENT_DOCKERFILES / "codex.Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:24-bookworm-slim" in content
    assert "npm install -g @openai/codex" in content
    assert 'ENTRYPOINT ["codex"]' in content
    assert "WORKDIR /workspace/project" in content


def test_opencode_agent_dockerfile_packages_opencode_cli():
    content = (AGENT_DOCKERFILES / "opencode.Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:24-bookworm-slim" in content
    assert "npm install -g opencode-ai" in content
    assert 'ENTRYPOINT ["opencode"]' in content
    assert "WORKDIR /workspace/project" in content


def test_agent_image_specs_point_at_packaged_dockerfiles():
    specs = list_agent_image_specs()

    assert {spec.id for spec in specs} == {"codex", "opencode"}
    for spec in specs:
        assert spec.dockerfile.is_file()
        assert spec.default_tag == f"open-tulid/agent-{spec.id}:latest"


def test_get_agent_image_spec_rejects_unknown_agent():
    with pytest.raises(ValueError, match="Unknown agent image"):
        get_agent_image_spec("missing")


def test_build_agent_image_invokes_docker_build_with_expected_tag():
    calls: list[tuple[str, ...]] = []

    def runner(args, *, check, capture_output, text):
        calls.append(tuple(args))
        assert check is False
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    result = build_agent_image("codex", runner=runner)
    spec = get_agent_image_spec("codex")

    assert result.succeeded is True
    assert result.tag == "open-tulid/agent-codex:latest"
    assert result.stdout == "ok"
    assert calls == [(
        "docker",
        "build",
        "-f",
        str(spec.dockerfile),
        "-t",
        "open-tulid/agent-codex:latest",
        str(spec.dockerfile.parent),
    )]


def test_build_agent_image_allows_custom_docker_and_tag_prefix():
    calls: list[tuple[str, ...]] = []

    def runner(args, *, check, capture_output, text):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    result = build_agent_image(
        "opencode",
        tag_prefix="registry.local/tulid",
        docker_executable="podman",
        runner=runner,
    )

    assert result.tag == "registry.local/tulid-opencode:latest"
    assert calls[0][0] == "podman"
    assert calls[0][5] == "registry.local/tulid-opencode:latest"


def test_build_agent_images_defaults_to_all_agents():
    calls: list[tuple[str, ...]] = []

    def runner(args, *, check, capture_output, text):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    results = build_agent_images(runner=runner)

    assert [result.agent_id for result in results] == ["codex", "opencode"]
    assert len(calls) == 2


def test_build_agent_image_reports_missing_docker():
    def runner(args, *, check, capture_output, text):
        raise FileNotFoundError("docker")

    result = build_agent_image("codex", runner=runner)

    assert result.succeeded is False
    assert result.returncode == 127
    assert "docker" in result.stderr


def test_agents_build_images_cli_builds_selected_agent(monkeypatch):
    calls: list[tuple[tuple[str, ...], str, str]] = []

    def fake_build_agent_images(agent_ids, *, tag_prefix, docker_executable):
        calls.append((tuple(agent_ids), tag_prefix, docker_executable))
        return (
            ImageBuildResult(
                agent_id="codex",
                tag="local/codex:latest",
                dockerfile=Path("codex.Dockerfile"),
                context_dir=Path("."),
                command=("docker", "build"),
                returncode=0,
            ),
        )

    monkeypatch.setattr(cli_main, "build_agent_images", fake_build_agent_images)

    result = runner.invoke(
        cli_main.app,
        [
            "agents",
            "build-images",
            "--agent",
            "codex",
            "--tag-prefix",
            "local/agent",
            "--docker",
            "podman",
        ],
    )

    assert result.exit_code == 0
    assert "Built codex: local/codex:latest" in result.output
    assert calls == [(("codex",), "local/agent", "podman")]


def test_agents_build_images_cli_rejects_unknown_agent():
    result = runner.invoke(cli_main.app, ["agents", "build-images", "--agent", "missing"])

    assert result.exit_code == 2
    assert "Unknown agent image" in result.output


def test_check_docker_reports_missing_cli():
    result = check_docker(which=lambda _cmd: None)

    assert result.available is False
    assert result.failure_reason == "docker_cli_missing"


def test_check_docker_reports_permission_denied():
    def fake_runner(args, *, check, capture_output, text):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="permission denied while trying to connect to Docker daemon",
        )

    result = check_docker(runner=fake_runner, which=lambda _cmd: "/usr/bin/docker")

    assert result.available is False
    assert result.failure_reason == "docker_daemon_permission_denied"


def test_docker_install_plan_for_debian_like_host(tmp_path: Path):
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nID_LIKE=debian\n', encoding="utf-8")

    plan = docker_install_plan(os_release)

    assert plan.supported is True
    assert any("docker-ce" in command for command in plan.commands)


def test_run_agent_container_builds_docker_run_command(tmp_path: Path):
    calls: list[tuple[str, ...]] = []

    def fake_runner(args, *, check, capture_output, text, timeout):
        calls.append(tuple(args))
        assert timeout == 30
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="done", stderr="")

    request = AgentRunRequest(
        agent_id="codex",
        image="open-tulid/agent-codex:latest",
        workspace=tmp_path,
        args=("run", "task"),
        env={"OPEN_TULID_JOB_ID": "JOB-123", "TOKEN": "x"},
        timeout_seconds=30,
        container_name="open-tulid-job-job-123",
    )

    result = run_agent_container(request, docker_executable="podman", runner=fake_runner)

    assert result.succeeded is True
    assert result.stdout == "done"
    assert calls == [(
        "podman",
        "run",
        "--rm",
        "--name",
        "open-tulid-job-job-123",
        "-v",
        f"{tmp_path.resolve()}:/workspace/project:rw",
        "-w",
        "/workspace/project",
        "-e",
        "OPEN_TULID_JOB_ID=JOB-123",
        "-e",
        "TOKEN=x",
        "open-tulid/agent-codex:latest",
        "run",
        "task",
    )]


def test_request_for_worker_uses_runtime_worker_image_override(tmp_path: Path):
    runtime = RuntimeConfig(
        worker_images={"codex": "registry.local/codex:dev"},
        env={"GLOBAL": "1"},
    )

    request = request_for_worker(
        worker_id="codex",
        workspace=tmp_path,
        runtime=runtime,
        args=("hello",),
        env={"OPEN_TULID_JOB_ID": "ABC123", "LOCAL": "2"},
    )

    assert request.image == "registry.local/codex:dev"
    assert request.env == {"GLOBAL": "1", "OPEN_TULID_JOB_ID": "ABC123", "LOCAL": "2"}
    assert request.container_name == "open-tulid-job-abc123"
    assert request.args == ("hello",)


def test_image_for_agent_defaults_to_tag_prefix():
    runtime = RuntimeConfig(image_tag_prefix="local/agent")

    assert image_for_agent("opencode", runtime) == "local/agent-opencode:latest"


def test_agents_doctor_cli_reports_available_docker(monkeypatch):
    monkeypatch.setattr(
        cli_main,
        "check_docker",
        lambda docker: DockerAvailability(
            available=True,
            docker_executable=docker,
            cli_found=True,
            daemon_reachable=True,
            user_can_access_daemon=True,
        ),
    )

    result = runner.invoke(cli_main.app, ["agents", "doctor"])

    assert result.exit_code == 0
    assert "Docker is available" in result.output


def test_install_docker_cli_defaults_to_dry_run(monkeypatch):
    class Plan:
        supported = True
        platform_id = "ubuntu"
        notes = ("note",)
        commands = (("sudo", "apt-get", "update"),)

    monkeypatch.setattr(cli_main, "docker_install_plan", lambda: Plan())

    result = runner.invoke(cli_main.app, ["install", "docker"])

    assert result.exit_code == 0
    assert "sudo apt-get update" in result.output
    assert "Dry run only" in result.output
