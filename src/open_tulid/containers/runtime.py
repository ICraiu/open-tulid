from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from open_tulid.models import RuntimeConfig

class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        ...


@dataclass(frozen=True)
class ContainerMount:
    host_path: Path
    container_path: str
    readonly: bool = False


@dataclass(frozen=True)
class AgentRunRequest:
    agent_id: str
    image: str
    workspace: Path
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[ContainerMount, ...] = ()
    extra_hosts: tuple[str, ...] = ()
    workdir: str = "/workspace/project"
    timeout_seconds: int | None = None
    remove: bool = True
    volume_relabel: bool = False
    container_name: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    agent_id: str
    image: str
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


def image_for_agent(agent_id: str, runtime: RuntimeConfig) -> str:
    configured = runtime.worker_images.get(agent_id)
    if configured:
        return configured
    return f"{runtime.image_tag_prefix}-{agent_id}:latest"


def request_for_worker(
    *,
    worker_id: str,
    workspace: Path,
    runtime: RuntimeConfig,
    args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    mounts: Sequence[ContainerMount] = (),
) -> AgentRunRequest:
    merged_env = {**runtime.env, **dict(env or {})}
    return AgentRunRequest(
        agent_id=worker_id,
        image=image_for_agent(worker_id, runtime),
        workspace=workspace,
        args=tuple(args),
        env=merged_env,
        mounts=tuple(mounts),
        extra_hosts=_runtime_extra_hosts(runtime),
        workdir=runtime.container_workspace,
        timeout_seconds=runtime.default_timeout_seconds,
        volume_relabel=runtime.container_volume_relabel,
        container_name=_job_container_name(merged_env),
    )


def run_agent_container(
    request: AgentRunRequest,
    *,
    docker_executable: str = "docker",
    runner: CommandRunner = subprocess.run,
) -> AgentRunResult:
    workspace = request.workspace.resolve()
    mounts = (ContainerMount(workspace, request.workdir), *request.mounts)
    command: list[str] = [docker_executable, "run"]
    if request.remove:
        command.append("--rm")
    if request.container_name:
        command.extend(["--name", request.container_name])
    for mount in mounts:
        mode = "ro" if mount.readonly else "rw"
        if request.volume_relabel:
            mode = f"{mode},z"
        command.extend(["-v", f"{mount.host_path.resolve()}:{mount.container_path}:{mode}"])
    for host in request.extra_hosts:
        command.extend(["--add-host", host])
    command.extend(["-w", request.workdir])
    for key, value in sorted(request.env.items()):
        command.extend(["-e", f"{key}={value}"])
    command.append(request.image)
    command.extend(request.args)

    try:
        completed = runner(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=request.timeout_seconds,
        )
    except FileNotFoundError as exc:
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=tuple(command),
            returncode=127,
            stderr=str(exc),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=tuple(command),
            returncode=124,
            stdout=stdout,
            stderr=stderr or f"Agent container timed out after {request.timeout_seconds} seconds",
        )

    return AgentRunResult(
        agent_id=request.agent_id,
        image=request.image,
        command=tuple(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def default_shared_workspace_root(runtime: RuntimeConfig, fallback: Path) -> Path:
    return runtime.shared_workspace_root or fallback / "workspaces"


def _runtime_extra_hosts(runtime: RuntimeConfig) -> tuple[str, ...]:
    if runtime.completion_container_host == "host.docker.internal":
        return ("host.docker.internal:host-gateway",)
    return ()


def _job_container_name(env: Mapping[str, str]) -> str | None:
    job_id = env.get("OPEN_TULID_JOB_ID", "").strip()
    if not job_id:
        return None
    return f"open-tulid-job-{job_id.lower()}"
