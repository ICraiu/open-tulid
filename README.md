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

The config file is `~/.open-tulid.toml`, created by `tulid init`.

Example config:

```toml
[vault]
root = "/path/to/obsidian/vault"
projects = ["Agent", "Game"]
```

## Usage

```bash
tulid --help
tulid init
tulid vault validate
tulid project <name>
tulid uninstall
```

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

This removes the app but keeps `~/.open-tulid.toml`.
