# open-tulid

A CLI application which coordinates coding agents using Obsidian.

## Requirements

- Python 3.11+

## Installation

```bash
pip install -e .
```

## Configuration

Create `open-tulid.toml` in your working directory:

```toml
[vault]
root = "/path/to/obsidian/vault"
projects = ["Agent", "Game"]
```

## Usage

```bash
tulid --help
tulid vault validate
tulid project <name>
```

## Building

This project uses [hatchling](https://hatch.pypa.io/) as the build backend.

```bash
pip install build
python -m build
```

The built distribution packages will be placed in `dist/`.

## Testing

```bash
pip install -e ".[test]"
pytest
```

To run tests with verbose output:

```bash
pytest -v
```

To run a specific test file:

```bash
pytest tests/test_vault_validate.py -v
pytest tests/test_project_create.py -v
```
