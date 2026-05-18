# App Installation Spec

## Purpose

Open Tulid installation must prepare both the Python CLI and the local agent
runtime prerequisites. Coding agents run in Docker images, so Docker must be
available before agent images can be built or workers can be executed.

## Current Scope

This spec covers installation behavior. It does not define workflow execution,
container scheduling, credentials, or workspace mount policy.

## Installation Requirements

The installer must verify these tools:

- Python 3.11+
- `pip` or `uv`
- Docker CLI
- Docker daemon access for the current user

The installer may install Docker when it is missing, but it must not silently
change the host. Docker installation requires explicit user consent because it
usually needs elevated privileges and may modify system services and groups.

## Docker Detection

Installation should treat Docker as unavailable when any of these checks fail:

```bash
command -v docker
docker version
docker info
```

Failure modes must be reported separately:

- Docker CLI missing.
- Docker daemon not running.
- Current user cannot access the daemon.
- Docker command exists but is not functional.

## Docker Installation Policy

When Docker is missing, the installer should offer an explicit install path.

Supported first target:

- Linux hosts using Docker Engine packages from Docker's official repository.

Future targets:

- macOS with Docker Desktop or Colima.
- Windows with Docker Desktop and WSL2.
- Rootless Docker.
- Podman-compatible mode.

The installer must prefer official Docker installation sources. It must not use
curl-piped shell scripts as the default unattended path.

## Linux Docker Install Flow

The installer should:

1. Detect distro family.
2. Show the package operations it is about to perform.
3. Ask for confirmation.
4. Install Docker Engine, Docker CLI, containerd, and buildx/plugin packages.
5. Enable and start the Docker service.
6. Optionally add the current user to the `docker` group.
7. Tell the user when a new login session is required for group membership.
8. Re-run Docker detection.

If any step fails, installation must stop with a clear diagnostic and leave the
Python CLI install state understandable.

## Agent Image Build Flow

After Docker is available, Open Tulid can build local agent images:

```bash
tulid agents build-images
```

Default image tags:

```text
open-tulid/agent-codex:latest
open-tulid/agent-opencode:latest
```

The command must support:

- Building all known agent images.
- Building one or more selected agents.
- Using a custom Docker executable.
- Using a custom tag prefix.

Image building is separate from container execution. The build command must not
decide workspace mounts or run agent jobs.

## Non-Goals

Do not install or configure API credentials.
Do not mount user repositories during image build.
Do not run agent containers during installation.
Do not infer workflow workers from Dockerfiles.

## Acceptance Criteria

- A fresh install can detect whether Docker is usable.
- If Docker is missing, the installer can guide or perform an approved Docker
  installation path.
- Agent image builds are exposed by the CLI.
- Agent image build failures include the Docker stderr output.
- Container execution remains a later layer that consumes compiled DSL workers
  and explicit runtime configuration.
