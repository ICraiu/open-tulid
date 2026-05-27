from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, Sequence

from open_tulid.domain import WorkflowDefinition
from open_tulid.models import ProjectConfig, RuntimeConfig

from .runtime import image_for_agent


PROJECT_DOCKERFILE = "Docker.tulid"
PROJECT_IMAGE_PREFIX = "open-tulid/project"


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
class ProjectImageBuildResult:
    project: str
    worker_id: str
    tag: str
    base_image: str
    dockerfile: Path
    context_dir: Path
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


def project_dockerfile_path(project_config: ProjectConfig, project_path: Path) -> Path:
    root = project_config.repo_root or project_path
    return root / PROJECT_DOCKERFILE


def project_worker_ids(workflow: WorkflowDefinition) -> tuple[str, ...]:
    worker_ids: set[str] = set()
    for transition in workflow.transitions.values():
        if transition.worker is None:
            continue
        worker = workflow.workers.get(transition.worker)
        worker_ids.add(worker.implementation_id if worker is not None and worker.implementation_id else transition.worker)
    return tuple(sorted(worker_ids))


def project_worker_image_tag(project: str, worker_id: str) -> str:
    return f"{PROJECT_IMAGE_PREFIX}-{_tag_part(project)}-{_tag_part(worker_id)}:latest"


def runtime_with_project_images(
    runtime: RuntimeConfig,
    *,
    project: str,
    worker_ids: Sequence[str],
) -> RuntimeConfig:
    worker_images = dict(runtime.worker_images)
    for worker_id in worker_ids:
        worker_images[worker_id] = project_worker_image_tag(project, worker_id)
    return replace(runtime, worker_images=worker_images)


def build_project_worker_image(
    *,
    project: str,
    worker_id: str,
    dockerfile: Path,
    runtime: RuntimeConfig,
    docker_executable: str = "docker",
    runner: CommandRunner = subprocess.run,
) -> ProjectImageBuildResult:
    tag = project_worker_image_tag(project, worker_id)
    base_image = image_for_agent(worker_id, runtime)
    context_dir = dockerfile.parent
    command = (
        docker_executable,
        "build",
        "-f",
        str(dockerfile),
        "--build-arg",
        f"TULID_AGENT_IMAGE={base_image}",
        "--label",
        f"open-tulid.project={project}",
        "--label",
        f"open-tulid.worker={worker_id}",
        "-t",
        tag,
        str(context_dir),
    )
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        return ProjectImageBuildResult(
            project=project,
            worker_id=worker_id,
            tag=tag,
            base_image=base_image,
            dockerfile=dockerfile,
            context_dir=context_dir,
            command=command,
            returncode=127,
            stderr=str(exc),
        )
    return ProjectImageBuildResult(
        project=project,
        worker_id=worker_id,
        tag=tag,
        base_image=base_image,
        dockerfile=dockerfile,
        context_dir=context_dir,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def build_project_worker_images(
    *,
    project: str,
    worker_ids: Sequence[str],
    dockerfile: Path,
    runtime: RuntimeConfig,
    docker_executable: str = "docker",
    runner: CommandRunner = subprocess.run,
) -> tuple[ProjectImageBuildResult, ...]:
    return tuple(
        build_project_worker_image(
            project=project,
            worker_id=worker_id,
            dockerfile=dockerfile,
            runtime=runtime,
            docker_executable=docker_executable,
            runner=runner,
        )
        for worker_id in worker_ids
    )


def _tag_part(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip(".-")
    return normalized or "unnamed"
