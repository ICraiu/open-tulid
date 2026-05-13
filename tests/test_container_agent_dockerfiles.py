from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_tulid.containers import (
    ImageBuildResult,
    build_agent_image,
    build_agent_images,
    get_agent_image_spec,
    list_agent_image_specs,
)
from open_tulid.cli import main as cli_main


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
