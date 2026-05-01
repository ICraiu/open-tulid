from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()


def init() -> None:
    """Create ~/.open-tulid.toml configuration file."""
    config_path = Path.home() / ".open-tulid.toml"

    if config_path.exists():
        console.print(Panel(
            f"Config already exists at {config_path}",
            style="yellow",
        ))
        raise SystemExit(1)

    content = "[vault]\nroot = \"/path/to/obsidian/vault\"\nprojects = [\"Agent\", \"Game\"]\n"
    config_path.write_text(content, encoding="utf-8")

    console.print(Panel(
        f"Config created at {config_path}",
        style="green",
    ))
