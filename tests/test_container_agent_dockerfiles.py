from __future__ import annotations

from pathlib import Path


AGENT_DOCKERFILES = Path("src/open_tulid/containers/agents")


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
