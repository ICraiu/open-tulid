"""Freeze and execute verification in the declared project environment (plan 4B).

The project global commands must run against a stable candidate in the same
resolved image identity the worker attempt used, never by executing submitted
project code directly on the Tulid host. This module provides:

- ``prepare_verification_copy``: materialize a writable verification copy from
  the immutable frozen candidate so commands that create caches/build output do
  not pollute the accepted bytes.
- ``resolve_working_directory``: resolve repository-relative command
  directories inside the copy, refusing escapes.
- ``capture_lockfile_identity`` / ``LockfileIdentity``: freeze the committed
  dependency sources so a dependency install cannot silently rewrite them.
- ``HostCommandExecutor``: the deterministic host subprocess strategy used by
  unit/e2e tests.
- ``ContainerCommandExecutor``: runs each global command in a disposable
  container built from the resolved project image, with the agent entrypoint
  overridden, resolving repository-relative paths inside the mounted copy and
  terminating the container on timeout/cancellation.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from open_tulid.domain import DomainError
from open_tulid.runtime.repository_facts import KNOWN_MANIFESTS, canonical_sha256
from open_tulid.runtime.verifier import VerificationCheckResult, VerificationCommand

# Committed dependency sources whose content must not change during verification.
LOCKFILE_NAMES = frozenset(KNOWN_MANIFESTS)

# Outcome of running one global command, bound to that exact command/workspace.
@dataclass(frozen=True)
class CommandExecutionOutcome:
    check: VerificationCheckResult
    error: DomainError | None = None


class VerificationCommandExecutor(Protocol):
    def execute(
        self,
        command: VerificationCommand,
        workspace: Path,
    ) -> CommandExecutionOutcome:
        ...


@dataclass(frozen=True)
class VerificationEnvironment:
    """Resolved project environment for verification (plan 4B)."""

    project_image_identity: str | None = None
    container_workspace: str = "/workspace/project"
    docker_executable: str = "docker"
    container_user: str | None = None
    container_volume_relabel: bool = False
    environment_identity: str | None = None
    # Documented project preparation step that provisions dependencies from
    # committed lockfiles/image build before application tests run.
    preparation_step: tuple[str, ...] = ()


def environment_identity_of(environment: VerificationEnvironment) -> str:
    body = {
        "schema": "tulid.environment/v1",
        "project_image_identity": environment.project_image_identity,
        "container_workspace": environment.container_workspace,
        "container_user": environment.container_user,
        "container_volume_relabel": environment.container_volume_relabel,
        "docker_executable": environment.docker_executable,
        "preparation_step": list(environment.preparation_step),
        "host_execution": environment.project_image_identity is None,
    }
    return canonical_sha256(body)


def prepare_verification_copy(
    *,
    candidate_path: Path,
    writable_root: Path,
    copy_id: str,
) -> Path:
    """Materialize a writable verification copy from an immutable candidate.

    The frozen candidate stays separately immutable; the copy is the only place
    commands may create caches/build output or mutate during verification.
    Repository-relative paths are resolved within this copy, never back to the
    candidate or the worker's mutable workspace.
    """
    copy_root = writable_root / copy_id
    if copy_root.exists():
        shutil.rmtree(copy_root)
    copy_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        Path(candidate_path),
        copy_root,
        dirs_exist_ok=False,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".open-tulid", "node_modules", "__pycache__"),
    )
    return copy_root


def resolve_working_directory(workspace: Path, relative: str) -> Path | None:
    """Resolve a repository-relative command directory inside ``workspace``.

    Absolute paths and ``..`` escapes are refused so submitted commands cannot
    run outside the verification copy.
    """
    if not relative or Path(relative).is_absolute():
        return None
    if ".." in Path(relative).parts:
        return None
    resolved = (workspace / relative).resolve()
    root = workspace.resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


@dataclass(frozen=True)
class LockfileIdentity:
    """Digest of the committed dependency sources at a point in time."""

    schema: str = "tulid.lockfile-identity/v1"
    paths: tuple[str, ...] = ()
    sha256: str = ""


def capture_lockfile_identity(workspace: Path) -> LockfileIdentity:
    """Freeze the committed lock/manifest files under ``workspace``.

    A later identity differing from this one means a dependency install rewrote
    committed locks, which is rejected rather than silently allowed.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        if path.name not in LOCKFILE_NAMES:
            continue
        relative = path.relative_to(workspace).as_posix()
        entries.append((relative, _file_sha256(path)))
    body = {
        "schema": "tulid.lockfile-identity/v1",
        "files": [{"path": path, "sha256": digest} for path, digest in entries],
    }
    return LockfileIdentity(
        paths=tuple(path for path, _ in entries),
        sha256=canonical_sha256(body),
    )


class HostCommandExecutor:
    """Deterministic host subprocess strategy (unit/e2e tests).

    Kept as the default so the deterministic suite exercises the same check
    semantics without depending on Docker or the declared project image. The
    production runtime uses :class:`ContainerCommandExecutor` instead.
    """

    def execute(
        self,
        command: VerificationCommand,
        workspace: Path,
    ) -> CommandExecutionOutcome:
        started = _now_utc_iso()
        cwd = resolve_working_directory(workspace, command.working_directory)
        expected = command.expected_exit_code
        if cwd is None or not cwd.is_dir():
            check = VerificationCheckResult(
                id=command.name,
                status="environment_error",
                argv=command.argv,
                stderr=_bounded_excerpt("working directory unavailable"),
                working_directory=command.working_directory,
                timeout_seconds=command.timeout_seconds,
                expected_exit_code=expected,
                started_at=started,
                ended_at=_now_utc_iso(),
            )
            return CommandExecutionOutcome(check, _error(
                "verification.check_environment",
                f"Verification command {command.name!r} has no usable working directory: {command.working_directory!r}.",
                command.name,
            ))
        start_ns = _monotonic_ns()
        try:
            completed = subprocess.run(
                command.argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            check = VerificationCheckResult(
                id=command.name,
                status="timeout",
                argv=command.argv,
                stdout=_bounded_excerpt(_as_text(exc.stdout)),
                stderr=_bounded_excerpt(_as_text(exc.stderr)),
                working_directory=command.working_directory,
                timeout_seconds=command.timeout_seconds,
                expected_exit_code=expected,
                started_at=started,
                ended_at=_now_utc_iso(),
                duration_seconds=_seconds_since(start_ns),
            )
            return CommandExecutionOutcome(check, _error(
                "verification.check_timeout",
                f"Verification command {command.name!r} timed out after {command.timeout_seconds}s: {_as_command_line(command.argv)}.",
                command.name,
            ))
        except OSError as exc:
            check = VerificationCheckResult(
                id=command.name,
                status="environment_error",
                argv=command.argv,
                stderr=_bounded_excerpt(str(exc)),
                working_directory=command.working_directory,
                timeout_seconds=command.timeout_seconds,
                expected_exit_code=expected,
                started_at=started,
                ended_at=_now_utc_iso(),
                duration_seconds=_seconds_since(start_ns),
            )
            return CommandExecutionOutcome(check, _error(
                "verification.check_environment",
                f"Verification command {command.name!r} could not be found or run ({_as_command_line(command.argv)}): {exc}",
                command.name,
            ))
        duration = _seconds_since(start_ns)
        stdout_ok = all(value in completed.stdout for value in ())
        stderr_ok = all(value in completed.stderr for value in ())
        passed = completed.returncode == expected and stdout_ok and stderr_ok
        check = VerificationCheckResult(
            id=command.name,
            status=VERIFICATION_PASSED if passed else "failed",
            argv=command.argv,
            exit_code=completed.returncode,
            stdout=_bounded_excerpt(completed.stdout),
            stderr=_bounded_excerpt(completed.stderr),
            working_directory=command.working_directory,
            timeout_seconds=command.timeout_seconds,
            expected_exit_code=expected,
            started_at=started,
            ended_at=_now_utc_iso(),
            duration_seconds=duration,
        )
        return CommandExecutionOutcome(
            check,
            None if passed else _error(
                "verification.check_failed",
                _check_failure_detail(command.name, expected, completed),
                command.name,
            ),
        )


class ContainerCommandExecutor:
    """Runs each global command inside a disposable project-image container.

    The verification copy is mounted at the project root; each command runs with
    the agent entrypoint overridden by ``/bin/sh`` and a working directory
    resolved from the repository-relative path. No model-proxy or completion
    credentials are passed. On timeout/cancellation the container is stopped and
    the interrupted command is recorded, never equated with success.
    """

    def __init__(
        self,
        *,
        environment: VerificationEnvironment,
        runner=None,
    ) -> None:
        from open_tulid.containers.runtime import (
            AgentRunRequest,
            run_agent_container,
        )

        self.environment = environment
        self._runner = runner if runner is not None else subprocess.run
        self._run_agent_container = run_agent_container
        self._agent_run_request = AgentRunRequest

    def execute(
        self,
        command: VerificationCommand,
        workspace: Path,
    ) -> CommandExecutionOutcome:
        env = self.environment
        started = _now_utc_iso()
        resolved = resolve_working_directory(workspace, command.working_directory)
        if resolved is None or not resolved.is_dir():
            check = VerificationCheckResult(
                id=command.name,
                status="environment_error",
                argv=command.argv,
                stderr=_bounded_excerpt("working directory unavailable"),
                working_directory=command.working_directory,
                timeout_seconds=command.timeout_seconds,
                expected_exit_code=command.expected_exit_code,
                started_at=started,
                ended_at=_now_utc_iso(),
            )
            return CommandExecutionOutcome(check, _error(
                "verification.check_environment",
                f"Verification command {command.name!r} has no usable working directory: {command.working_directory!r}.",
                command.name,
            ))
        if env.project_image_identity is None:
            check = VerificationCheckResult(
                id=command.name,
                status="environment_error",
                argv=command.argv,
                stderr=_bounded_excerpt("no resolved project image for verification"),
                working_directory=command.working_directory,
                timeout_seconds=command.timeout_seconds,
                expected_exit_code=command.expected_exit_code,
                started_at=started,
                ended_at=_now_utc_iso(),
            )
            return CommandExecutionOutcome(check, _error(
                "verification.env_image_unavailable",
                f"Verification cannot run in a container: no resolved project image identity is configured for {command.name!r}.",
                command.name,
            ))
        relative_parts = Path(command.working_directory).parts if command.working_directory not in ("", ".") else ()
        container_cwd = "/".join((env.container_workspace.rstrip("/"), *relative_parts))
        container_name = f"open-tulid-verify-{command.name}"
        request = self._agent_run_request(
            agent_id="verifier",
            image=env.project_image_identity,
            workspace=workspace,
            args=("-c", shlex.join(command.argv)),
            entrypoint=("/bin/sh",),
            env={"OPEN_TULID_VERIFICATION": "1"},
            workdir=container_cwd,
            mount_container_path=env.container_workspace,
            timeout_seconds=command.timeout_seconds,
            container_name=container_name,
            container_user=env.container_user,
            volume_relabel=env.container_volume_relabel,
        )
        start_ns = _monotonic_ns()
        result = self._run_agent_container(
            request,
            docker_executable=env.docker_executable,
            runner=self._runner,
        )
        duration = _seconds_since(start_ns)
        ended_at = _now_utc_iso()
        if result.returncode == 124:
            # Timeout/cancellation: terminate the container and record the
            # interrupted command so it can never count as success.
            self._terminate_container(container_name)
            check = VerificationCheckResult(
                id=command.name,
                status="timeout",
                argv=command.argv,
                stdout=_bounded_excerpt(result.stdout),
                stderr=_bounded_excerpt(result.stderr or f"Verification container for {command.name!r} timed out after {command.timeout_seconds}s"),
                working_directory=command.working_directory,
                timeout_seconds=command.timeout_seconds,
                expected_exit_code=command.expected_exit_code,
                started_at=started,
                ended_at=ended_at,
                duration_seconds=duration,
            )
            return CommandExecutionOutcome(check, _error(
                "verification.check_timeout",
                f"Verification command {command.name!r} timed out after {command.timeout_seconds}s; the container was terminated.",
                command.name,
            ))
        if result.returncode in (127, 126) and _docker_unavailable(result):
            check = VerificationCheckResult(
                id=command.name,
                status="environment_error",
                argv=command.argv,
                stderr=_bounded_excerpt(result.stderr or result.stdout),
                working_directory=command.working_directory,
                timeout_seconds=command.timeout_seconds,
                expected_exit_code=command.expected_exit_code,
                started_at=started,
                ended_at=ended_at,
                duration_seconds=duration,
            )
            return CommandExecutionOutcome(check, _error(
                "verification.env_image_unavailable",
                f"Verification cannot start container for {command.name!r}: {result.stderr or result.stdout or 'container environment unavailable'}",
                command.name,
            ))
        passed = result.returncode == command.expected_exit_code
        check = VerificationCheckResult(
            id=command.name,
            status=VERIFICATION_PASSED if passed else "failed",
            argv=command.argv,
            exit_code=result.returncode,
            stdout=_bounded_excerpt(result.stdout),
            stderr=_bounded_excerpt(result.stderr),
            working_directory=command.working_directory,
            timeout_seconds=command.timeout_seconds,
            expected_exit_code=command.expected_exit_code,
            started_at=started,
            ended_at=ended_at,
            duration_seconds=duration,
        )
        return CommandExecutionOutcome(check, None if passed else _error(
            "verification.check_failed",
            _container_failure_detail(command.name, command.expected_exit_code, result),
            command.name,
        ))

    def _terminate_container(self, container_name: str) -> None:
        env = self.environment
        for args in (
            (env.docker_executable, "stop", container_name),
            (env.docker_executable, "rm", "-f", container_name),
        ):
            try:
                if self._runner is not None:
                    self._runner(args, check=False, capture_output=True, text=True)
                else:
                    subprocess.run(args, capture_output=True, text=True, check=False)
            except (OSError, subprocess.SubprocessError):
                continue


def _docker_unavailable(result) -> bool:
    blob = f"{result.stderr or ''}{result.stdout or ''}".lower()
    return "no such container" in blob or "cannot connect" in blob or "not found" in blob


def _container_failure_detail(
    check_id: str,
    expected_exit_code: int | None,
    result,
) -> str:
    reasons = [f"exit code {result.returncode} (expected {expected_exit_code or 0})"]
    tail = "\n".join((result.stdout or "", result.stderr or "")).strip().splitlines()[-5:]
    evidence = (" ".join(line.strip() for line in tail) if tail else "no output")
    return (
        f"Verification command {check_id!r} failed ({'; '.join(reasons)}; "
        f"{len(tail)} output line(s) captured: {evidence[:400]})"
    )


def _bounded_excerpt(value: str) -> str:
    from open_tulid.runtime.verifier import LOG_EXCERPT_CHARACTER_LIMIT
    value = value or ""
    if len(value) <= LOG_EXCERPT_CHARACTER_LIMIT:
        return value
    marker = f"\n[... {len(value) - LOG_EXCERPT_CHARACTER_LIMIT} characters omitted; full log retained as artifact]"
    return value[: LOG_EXCERPT_CHARACTER_LIMIT - len(marker)] + marker


def _check_failure_detail(
    check_id: str,
    expected_exit_code: int | None,
    completed: subprocess.CompletedProcess[str],
) -> str:
    reasons: list[str] = []
    if completed.returncode != expected_exit_code:
        reasons.append(
            f"exit code {completed.returncode} (expected {expected_exit_code or 0})"
        )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    tail = "\n".join((stdout, stderr)).strip().splitlines()[-5:]
    evidence = (" ".join(line.strip() for line in tail) if tail else "no output")
    details = "; ".join(reasons) if reasons else "exit expectation was not met"
    line_count = len(tail)
    return (
        f"Verification command {check_id!r} failed ({details}; "
        f"{line_count} output line(s) captured: {evidence[:400]})"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _as_command_line(argv: Sequence[str]) -> str:
    return shlex.join(argv)


def _monotonic_ns() -> int:
    import time
    return time.monotonic_ns()


def _seconds_since(started_ns: int) -> float:
    import time
    return max(0.0, (time.monotonic_ns() - started_ns) / 1_000_000_000)


def _now_utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


# Re-export for convenience.
VERIFICATION_PASSED = "passed"
