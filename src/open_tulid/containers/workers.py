from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from open_tulid.models import RuntimeConfig

from .runtime import AgentRunResult, request_for_worker, run_agent_container


def run_codex_worker(
    *,
    workspace: Path,
    runtime: RuntimeConfig,
    args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> AgentRunResult:
    request = request_for_worker(
        worker_id="codex",
        workspace=workspace,
        runtime=runtime,
        args=args,
        env=env,
    )
    return run_agent_container(request, docker_executable=runtime.docker_executable)


def run_opencode_worker(
    *,
    workspace: Path,
    runtime: RuntimeConfig,
    args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> AgentRunResult:
    request = request_for_worker(
        worker_id="opencode",
        workspace=workspace,
        runtime=runtime,
        args=args,
        env=env,
    )
    return run_agent_container(request, docker_executable=runtime.docker_executable)
