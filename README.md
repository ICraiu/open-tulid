# open-tulid

A CLI application which coordinates coding agents using Obsidian.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (for building)

## Installation

```bash
./install.sh
```

This installs the app and runs initialization.

Or manually:

```bash
pip install -e .
tulid init
```

## Configuration

The config file is `~/.tulid/config.yaml`, created by `tulid init`.

For a full setup walkthrough, including project layout, agent instruction files,
runtime directories, worker configuration, and the first end-to-end run, see:

```text
docs/startup-guide.md
```

Example config:

```yaml
tracker:
  type: obsidian
  root: /path/to/tracker/root
projects:
  Agent:
    path: Agent
    repo_root: /path/to/code/repository
    main_branch: main
```

## Usage

```bash
tulid --help
tulid init
tulid validate
tulid vault validate
tulid project <name>
tulid tasks list <project>
tulid tasks runnable <project>
tulid transition <project> <task-id> <transition-id>
tulid jobs schedule <project>
tulid jobs run-one <project>
tulid jobs list <project>
tulid jobs show <project> <job-id>
tulid jobs logs <project> <job-id>
tulid transactions list <project>
tulid transactions recover <project>
tulid runtime start <project>
tulid runtime status <project>
tulid runtime stop <project>
tulid agents doctor
tulid agents build-images
tulid install docker
tulid uninstall
```

At a high level:

```text
validate project state
-> schedule one runnable transition
-> run a sandboxed worker
-> accept only trusted completion evidence
-> move the task through trusted runtime code
```

`docs/project-remaining-work-analysis-2026-05-17.md` tracks the remaining
hardening work; the current runtime is useful, but it is still intentionally
conservative about the security guarantees it claims.

## Agent Images

Open Tulid includes Dockerfiles for local coding-agent images.

```bash
tulid agents build-images
tulid agents build-images --agent codex
tulid agents build-images --agent opencode --tag-prefix registry.local/open-tulid/agent
```

Docker must be installed and usable by the current user. See
`docs/app-installation-spec.md` for the planned installation checks and Docker
installation flow.

Use `tulid agents doctor` to check Docker availability. Use
`tulid install docker` to print the guarded host-specific Docker install plan;
it defaults to a dry run.

## Building

This project uses [hatchling](https://hatch.pypa.io/) as the build backend.

```bash
./build.sh
```

This will build the package and run all tests.

Alternatively, to build without running tests:

```bash
# With uv
uv build

# With pip
pip install build && python -m build
```

The built distribution packages will be placed in `dist/`.

## Testing

```bash
python -m pytest -v
```

To run a specific test file:

```bash
python -m pytest tests/test_vault_validate.py -v
python -m pytest tests/test_project_create.py -v
```

## Uninstalling

```bash
tulid uninstall
```

This removes the app but keeps `~/.tulid/`.
