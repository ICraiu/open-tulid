from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from open_tulid.domain import WorkflowDefinition
from open_tulid.models import ProjectConfig, RuntimeConfig

from .docker import DockerAvailability, DockerInstallPlan, check_docker, docker_install_plan
from .images import (
    DEFAULT_TAG_PREFIX,
    AgentImageSpec,
    ImageBuildResult,
    build_agent_image,
    build_agent_images,
    get_agent_image_spec,
    list_agent_image_specs,
)
from .project_images import (
    ProjectImageBuildResult,
    build_project_worker_image,
    build_project_worker_images,
    project_dockerfile_path,
    project_worker_ids,
    project_worker_image_tag,
    runtime_with_project_images,
)
from .runtime import (
    AgentRunRequest,
    AgentRunResult,
    ContainerMount,
    default_shared_workspace_root,
    image_for_agent,
    request_for_worker,
    run_agent_container,
)
from .workers import run_codex_worker, run_opencode_worker


@runtime_checkable
class ContainersService(Protocol):
    def check_docker(self, docker_executable: str = "docker") -> DockerAvailability: ...

    def docker_install_plan(self, os_release_path: Path = Path("/etc/os-release")) -> DockerInstallPlan: ...

    def list_agent_image_specs(
        self,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
    ) -> tuple[AgentImageSpec, ...]: ...

    def get_agent_image_spec(
        self,
        agent_id: str,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
    ) -> AgentImageSpec: ...

    def build_agent_image(
        self,
        agent_id: str,
        *,
        tag: str | None = None,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> ImageBuildResult: ...

    def build_agent_images(
        self,
        agent_ids: Sequence[str] | None = None,
        *,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> tuple[ImageBuildResult, ...]: ...

    def project_dockerfile_path(self, project_config: ProjectConfig, project_path: Path) -> Path: ...

    def project_worker_ids(self, workflow: WorkflowDefinition) -> tuple[str, ...]: ...

    def project_worker_image_tag(self, project: str, worker_id: str) -> str: ...

    def runtime_with_project_images(
        self,
        runtime: RuntimeConfig,
        *,
        project: str,
        worker_ids: Sequence[str],
    ) -> RuntimeConfig: ...

    def build_project_worker_image(
        self,
        *,
        project: str,
        worker_id: str,
        dockerfile: Path,
        runtime: RuntimeConfig,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> ProjectImageBuildResult: ...

    def build_project_worker_images(
        self,
        *,
        project: str,
        worker_ids: Sequence[str],
        dockerfile: Path,
        runtime: RuntimeConfig,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> tuple[ProjectImageBuildResult, ...]: ...

    def default_shared_workspace_root(self, runtime: RuntimeConfig, fallback: Path) -> Path: ...

    def image_for_agent(self, agent_id: str, runtime: RuntimeConfig) -> str: ...

    def request_for_worker(
        self,
        *,
        worker_id: str,
        workspace: Path,
        runtime: RuntimeConfig,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        mounts: Sequence[ContainerMount] = (),
    ) -> AgentRunRequest: ...

    def run_agent_container(
        self,
        request: AgentRunRequest,
        *,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
        log_dir: Path | None = None,
    ) -> AgentRunResult: ...

    def run_codex_worker(
        self,
        *,
        workspace: Path,
        runtime: RuntimeConfig,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> AgentRunResult: ...

    def run_opencode_worker(
        self,
        *,
        workspace: Path,
        runtime: RuntimeConfig,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> AgentRunResult: ...


class DefaultContainersService:
    def check_docker(self, docker_executable: str = "docker") -> DockerAvailability:
        return check_docker(docker_executable)

    def docker_install_plan(self, os_release_path: Path = Path("/etc/os-release")) -> DockerInstallPlan:
        return docker_install_plan(os_release_path)

    def list_agent_image_specs(self, tag_prefix: str = DEFAULT_TAG_PREFIX) -> tuple[AgentImageSpec, ...]:
        return list_agent_image_specs(tag_prefix=tag_prefix)

    def get_agent_image_spec(self, agent_id: str, tag_prefix: str = DEFAULT_TAG_PREFIX) -> AgentImageSpec:
        return get_agent_image_spec(agent_id, tag_prefix=tag_prefix)

    def build_agent_image(
        self,
        agent_id: str,
        *,
        tag: str | None = None,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> ImageBuildResult:
        return build_agent_image(
            agent_id,
            tag=tag,
            tag_prefix=tag_prefix,
            docker_executable=docker_executable,
            runner=runner,
        )

    def build_agent_images(
        self,
        agent_ids: Sequence[str] | None = None,
        *,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> tuple[ImageBuildResult, ...]:
        return build_agent_images(
            agent_ids,
            tag_prefix=tag_prefix,
            docker_executable=docker_executable,
            runner=runner,
        )

    def project_dockerfile_path(self, project_config: ProjectConfig, project_path: Path) -> Path:
        return project_dockerfile_path(project_config, project_path)

    def project_worker_ids(self, workflow: WorkflowDefinition) -> tuple[str, ...]:
        return project_worker_ids(workflow)

    def project_worker_image_tag(self, project: str, worker_id: str) -> str:
        return project_worker_image_tag(project, worker_id)

    def runtime_with_project_images(
        self,
        runtime: RuntimeConfig,
        *,
        project: str,
        worker_ids: Sequence[str],
    ) -> RuntimeConfig:
        return runtime_with_project_images(runtime, project=project, worker_ids=worker_ids)

    def build_project_worker_image(
        self,
        *,
        project: str,
        worker_id: str,
        dockerfile: Path,
        runtime: RuntimeConfig,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> ProjectImageBuildResult:
        return build_project_worker_image(
            project=project,
            worker_id=worker_id,
            dockerfile=dockerfile,
            runtime=runtime,
            docker_executable=docker_executable,
            runner=runner,
        )

    def build_project_worker_images(
        self,
        *,
        project: str,
        worker_ids: Sequence[str],
        dockerfile: Path,
        runtime: RuntimeConfig,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
    ) -> tuple[ProjectImageBuildResult, ...]:
        return build_project_worker_images(
            project=project,
            worker_ids=worker_ids,
            dockerfile=dockerfile,
            runtime=runtime,
            docker_executable=docker_executable,
            runner=runner,
        )

    def default_shared_workspace_root(self, runtime: RuntimeConfig, fallback: Path) -> Path:
        return default_shared_workspace_root(runtime, fallback)

    def image_for_agent(self, agent_id: str, runtime: RuntimeConfig) -> str:
        return image_for_agent(agent_id, runtime)

    def request_for_worker(
        self,
        *,
        worker_id: str,
        workspace: Path,
        runtime: RuntimeConfig,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        mounts: Sequence[ContainerMount] = (),
    ) -> AgentRunRequest:
        return request_for_worker(
            worker_id=worker_id,
            workspace=workspace,
            runtime=runtime,
            args=args,
            env=env,
            mounts=mounts,
        )

    def run_agent_container(
        self,
        request: AgentRunRequest,
        *,
        docker_executable: str = "docker",
        runner: object = subprocess.run,
        log_dir: Path | None = None,
    ) -> AgentRunResult:
        return run_agent_container(
            request,
            docker_executable=docker_executable,
            runner=runner,
            log_dir=log_dir,
        )

    def run_codex_worker(
        self,
        *,
        workspace: Path,
        runtime: RuntimeConfig,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> AgentRunResult:
        return run_codex_worker(workspace=workspace, runtime=runtime, args=args, env=env)

    def run_opencode_worker(
        self,
        *,
        workspace: Path,
        runtime: RuntimeConfig,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> AgentRunResult:
        return run_opencode_worker(workspace=workspace, runtime=runtime, args=args, env=env)


def build_containers_service() -> ContainersService:
    return DefaultContainersService()


__all__ = ["ContainersService", "DefaultContainersService", "build_containers_service"]
