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

The config file is `~/.tuluid/open-tulid.toml`, created by `tulid init`.

Example config:

```toml
[vault]
root = "/path/to/obsidian/vault"
projects = ["Agent", "Game"]

[workflow]
path = "workflow.yaml"
```

## Usage

```bash
tulid --help
tulid init
tulid vault validate
tulid project <name>
tulid agents doctor
tulid agents build-images
tulid install docker
tulid uninstall
```

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

This removes the app but keeps `~/.tuluid/`.
