from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Iterator


DEFAULT_PROJECT_TEMPLATE = "templates/default_project"


def default_workflow_text() -> str:
    return _template_root().joinpath("workflow.yaml").read_text(encoding="utf-8")


def copy_default_project_scaffold(project_path: Path) -> None:
    for relative_path, source in _iter_template_files(_template_root()):
        target = project_path.joinpath(*relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _template_root() -> resources.abc.Traversable:
    return resources.files("open_tulid").joinpath(*DEFAULT_PROJECT_TEMPLATE.split("/"))


def _iter_template_files(
    root: resources.abc.Traversable,
    prefix: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], resources.abc.Traversable]]:
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        relative_path = (*prefix, child.name)
        if child.is_dir():
            yield from _iter_template_files(child, relative_path)
        elif child.is_file():
            yield relative_path, child
