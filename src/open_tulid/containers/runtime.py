from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Mapping, Protocol, Sequence, TextIO

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
    container_user: str | None = None


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
    image = image_for_agent(worker_id, runtime)
    worker_mounts = tuple(mounts)
    container_user = None
    if _is_opencode_worker(worker_id, runtime, image=image):
        debug_mounts, debug_env = _opencode_debug_config(workspace, runtime.container_workspace)
        worker_mounts = (*worker_mounts, *debug_mounts)
        merged_env = {**debug_env, **merged_env}
        container_user = _host_container_user()
    return AgentRunRequest(
        agent_id=worker_id,
        image=image,
        workspace=workspace,
        args=tuple(args),
        env=merged_env,
        mounts=worker_mounts,
        extra_hosts=_runtime_extra_hosts(runtime),
        workdir=runtime.container_workspace,
        timeout_seconds=runtime.default_timeout_seconds,
        volume_relabel=runtime.container_volume_relabel,
        container_name=_job_container_name(merged_env),
        container_user=container_user,
    )


def run_agent_container(
    request: AgentRunRequest,
    *,
    docker_executable: str = "docker",
    runner: CommandRunner = subprocess.run,
    log_dir: Path | None = None,
) -> AgentRunResult:
    workspace = request.workspace.resolve()
    mounts = (ContainerMount(workspace, request.workdir), *request.mounts)
    command: list[str] = [docker_executable, "run"]
    if request.remove:
        command.append("--rm")
    if request.container_name:
        command.extend(["--name", request.container_name])
    if request.container_user:
        command.extend(["--user", request.container_user])
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

    if log_dir is not None and runner is subprocess.run:
        return _run_agent_container_streaming(request, tuple(command), log_dir)

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


def _run_agent_container_streaming(
    request: AgentRunRequest,
    command: tuple[str, ...],
    log_dir: Path,
) -> AgentRunResult:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    agent_path = log_dir / "agent.log"
    try:
        with (
            stdout_path.open("w", encoding="utf-8") as stdout,
            stderr_path.open("w", encoding="utf-8") as stderr,
            agent_path.open("w", encoding="utf-8") as agent,
        ):
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            lock = threading.Lock()
            threads = (
                threading.Thread(
                    target=_copy_stream,
                    args=(_text_stream(process.stdout), stdout, agent, lock),
                    daemon=True,
                ),
                threading.Thread(
                    target=_copy_stream,
                    args=(_text_stream(process.stderr), stderr, agent, lock),
                    daemon=True,
                ),
            )
            for thread in threads:
                thread.start()
            try:
                returncode = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                message = f"Agent container timed out after {request.timeout_seconds} seconds\n"
                stderr.write(message)
                agent.write(message)
                returncode = 124
            for thread in threads:
                thread.join(timeout=5)
    except FileNotFoundError as exc:
        return AgentRunResult(
            agent_id=request.agent_id,
            image=request.image,
            command=command,
            returncode=127,
            stderr=str(exc),
        )

    return AgentRunResult(
        agent_id=request.agent_id,
        image=request.image,
        command=command,
        returncode=returncode,
        stdout=stdout_path.read_text(encoding="utf-8") if stdout_path.is_file() else "",
        stderr=stderr_path.read_text(encoding="utf-8") if stderr_path.is_file() else "",
    )


def _copy_stream(source: TextIO, stream_log: IO[str], agent_log: IO[str], lock: threading.Lock) -> None:
    while True:
        chunk = source.read(1)
        if chunk == "":
            return
        with lock:
            stream_log.write(chunk)
            stream_log.flush()
            agent_log.write(chunk)
            agent_log.flush()


def _text_stream(stream: TextIO | None) -> TextIO:
    assert stream is not None
    return stream


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


def _opencode_debug_config(workspace: Path, container_workspace: str) -> tuple[tuple[ContainerMount, ...], dict[str, str]]:
    context_root = workspace / ".open-tulid"
    data_root = workspace / ".open-tulid" / "opencode-data"
    home_root = workspace / ".open-tulid" / "home"
    data_root.mkdir(parents=True, exist_ok=True)
    share_root = home_root / ".local" / "share"
    share_root.mkdir(parents=True, exist_ok=True)
    link = share_root / "opencode"
    if not link.exists():
        try:
            link.symlink_to(f"{container_workspace}/.open-tulid/opencode-data")
        except OSError:
            pass
    container_context = f"{container_workspace}/.open-tulid"
    return (
        (),
        {
            "HOME": f"{container_context}/home",
            "OPENCODE_LOG_LEVEL": "debug",
            "XDG_DATA_HOME": f"{container_context}/home/.local/share",
        },
    )


def _is_opencode_worker(worker_id: str, runtime: RuntimeConfig, *, image: str | None = None) -> bool:
    if worker_id == "opencode" or runtime.worker_types.get(worker_id) == "opencode":
        return True
    resolved_image = image if image is not None else image_for_agent(worker_id, runtime)
    return "opencode" in Path(resolved_image).name


def _host_container_user() -> str | None:
    if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return None
    return f"{os.getuid()}:{os.getgid()}"
