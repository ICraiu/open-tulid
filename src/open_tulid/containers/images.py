from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


DEFAULT_TAG_PREFIX = "open-tulid/agent"


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        ...


@dataclass(frozen=True)
class AgentImageSpec:
    id: str
    dockerfile: Path
    default_tag: str


@dataclass(frozen=True)
class ImageBuildResult:
    agent_id: str
    tag: str
    dockerfile: Path
    context_dir: Path
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


def _agent_dockerfile_dir() -> Path:
    return Path(__file__).resolve().parent / "agents"


def list_agent_image_specs(tag_prefix: str = DEFAULT_TAG_PREFIX) -> tuple[AgentImageSpec, ...]:
    dockerfile_dir = _agent_dockerfile_dir()
    return (
        AgentImageSpec(
            id="codex",
            dockerfile=dockerfile_dir / "codex.Dockerfile",
            default_tag=f"{tag_prefix}-codex:latest",
        ),
        AgentImageSpec(
            id="opencode",
            dockerfile=dockerfile_dir / "opencode.Dockerfile",
            default_tag=f"{tag_prefix}-opencode:latest",
        ),
    )


def get_agent_image_spec(agent_id: str, tag_prefix: str = DEFAULT_TAG_PREFIX) -> AgentImageSpec:
    for spec in list_agent_image_specs(tag_prefix=tag_prefix):
        if spec.id == agent_id:
            return spec
    valid = ", ".join(spec.id for spec in list_agent_image_specs(tag_prefix=tag_prefix))
    raise ValueError(f"Unknown agent image {agent_id!r}. Valid agents: {valid}")


def build_agent_image(
    agent_id: str,
    *,
    tag: str | None = None,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    docker_executable: str = "docker",
    runner: CommandRunner = subprocess.run,
) -> ImageBuildResult:
    spec = get_agent_image_spec(agent_id, tag_prefix=tag_prefix)
    image_tag = tag or spec.default_tag
    context_dir = spec.dockerfile.parent
    command = (
        docker_executable,
        "build",
        "-f",
        str(spec.dockerfile),
        "-t",
        image_tag,
        str(context_dir),
    )
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        return ImageBuildResult(
            agent_id=spec.id,
            tag=image_tag,
            dockerfile=spec.dockerfile,
            context_dir=context_dir,
            command=command,
            returncode=127,
            stderr=str(exc),
        )
    return ImageBuildResult(
        agent_id=spec.id,
        tag=image_tag,
        dockerfile=spec.dockerfile,
        context_dir=context_dir,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def build_agent_images(
    agent_ids: Sequence[str] | None = None,
    *,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    docker_executable: str = "docker",
    runner: CommandRunner = subprocess.run,
) -> tuple[ImageBuildResult, ...]:
    selected = tuple(agent_ids) if agent_ids is not None else tuple(
        spec.id for spec in list_agent_image_specs(tag_prefix=tag_prefix)
    )
    return tuple(
        build_agent_image(
            agent_id,
            tag_prefix=tag_prefix,
            docker_executable=docker_executable,
            runner=runner,
        )
        for agent_id in selected
    )
