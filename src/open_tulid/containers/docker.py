from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


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
class DockerAvailability:
    available: bool
    docker_executable: str
    cli_found: bool
    daemon_reachable: bool
    user_can_access_daemon: bool
    version_stdout: str = ""
    info_stdout: str = ""
    error: str = ""

    @property
    def failure_reason(self) -> str | None:
        if self.available:
            return None
        if not self.cli_found:
            return "docker_cli_missing"
        if not self.daemon_reachable:
            return "docker_daemon_unreachable"
        if not self.user_can_access_daemon:
            return "docker_daemon_permission_denied"
        return "docker_unavailable"


@dataclass(frozen=True)
class DockerInstallPlan:
    supported: bool
    platform_id: str
    commands: tuple[tuple[str, ...], ...]
    notes: tuple[str, ...] = ()


def check_docker(
    docker_executable: str = "docker",
    *,
    runner: CommandRunner = subprocess.run,
    which: object = shutil.which,
) -> DockerAvailability:
    resolved = which(docker_executable)  # type: ignore[operator]
    if resolved is None:
        return DockerAvailability(
            available=False,
            docker_executable=docker_executable,
            cli_found=False,
            daemon_reachable=False,
            user_can_access_daemon=False,
            error=f"Docker CLI not found: {docker_executable}",
        )

    version = _run((docker_executable, "version"), runner)
    if version.returncode != 0:
        error = _combined_error(version)
        permission_denied = _looks_like_permission_denied(error)
        return DockerAvailability(
            available=False,
            docker_executable=docker_executable,
            cli_found=True,
            daemon_reachable=permission_denied,
            user_can_access_daemon=not permission_denied,
            version_stdout=version.stdout,
            error=error,
        )

    info = _run((docker_executable, "info"), runner)
    if info.returncode != 0:
        error = _combined_error(info)
        permission_denied = _looks_like_permission_denied(error)
        return DockerAvailability(
            available=False,
            docker_executable=docker_executable,
            cli_found=True,
            daemon_reachable=permission_denied,
            user_can_access_daemon=not permission_denied,
            version_stdout=version.stdout,
            info_stdout=info.stdout,
            error=error,
        )

    return DockerAvailability(
        available=True,
        docker_executable=docker_executable,
        cli_found=True,
        daemon_reachable=True,
        user_can_access_daemon=True,
        version_stdout=version.stdout,
        info_stdout=info.stdout,
    )


def docker_install_plan(os_release_path: Path = Path("/etc/os-release")) -> DockerInstallPlan:
    os_id, os_like = _read_os_release(os_release_path)
    system = platform.system().lower()
    if system != "linux":
        return DockerInstallPlan(
            supported=False,
            platform_id=system or "unknown",
            commands=(),
            notes=("Automatic Docker installation is currently specified only for Linux.",),
        )

    family = os_id or os_like
    if os_id in {"ubuntu", "debian"}:
        docker_repo = f"https://download.docker.com/linux/{os_id}"
        return DockerInstallPlan(
            supported=True,
            platform_id=os_id,
            commands=(
                ("sudo", "apt-get", "update"),
                ("sudo", "apt-get", "install", "-y", "ca-certificates", "curl"),
                ("sudo", "install", "-m", "0755", "-d", "/etc/apt/keyrings"),
                ("sudo", "curl", "-fsSL", f"{docker_repo}/gpg", "-o", "/etc/apt/keyrings/docker.asc"),
                ("sudo", "chmod", "a+r", "/etc/apt/keyrings/docker.asc"),
                (
                    "sudo",
                    "sh",
                    "-c",
                    f'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] {docker_repo} $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list',
                ),
                ("sudo", "apt-get", "update"),
                ("sudo", "apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"),
                ("sudo", "systemctl", "enable", "--now", "docker"),
            ),
            notes=(
                "Installs Docker Engine from Docker's official apt repository.",
                "Adding the current user to the docker group may require a new login session.",
            ),
        )

    if "debian" in os_like.split():
        return DockerInstallPlan(
            supported=False,
            platform_id=os_id or "debian-like",
            commands=(),
            notes=("Debian-like derivatives need explicit distro/codename mapping before installing Docker packages.",),
        )

    if family in {"fedora", "centos", "rhel"} or any(
        item in os_like.split() for item in ("fedora", "rhel")
    ):
        return DockerInstallPlan(
            supported=True,
            platform_id=os_id or "rhel",
            commands=(
                ("sudo", "dnf", "-y", "install", "dnf-plugins-core"),
                ("sudo", "dnf", "config-manager", "--add-repo", "https://download.docker.com/linux/centos/docker-ce.repo"),
                ("sudo", "dnf", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"),
                ("sudo", "systemctl", "enable", "--now", "docker"),
            ),
            notes=("RHEL-compatible hosts should use Docker's official rpm repository.",),
        )

    return DockerInstallPlan(
        supported=False,
        platform_id=os_id or "linux",
        commands=(),
        notes=("This Linux distribution is not supported by the installer yet.",),
    )


def _run(args: tuple[str, ...], runner: CommandRunner) -> subprocess.CompletedProcess[str]:
    try:
        return runner(args, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))


def _combined_error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"Command failed with exit code {result.returncode}").strip()


def _looks_like_permission_denied(message: str) -> bool:
    lowered = message.lower()
    return "permission denied" in lowered or "got permission denied" in lowered


def _read_os_release(path: Path) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", ""
    data: dict[str, str] = {}
    for line in lines:
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data.get("ID", "").lower(), data.get("ID_LIKE", "").lower()
