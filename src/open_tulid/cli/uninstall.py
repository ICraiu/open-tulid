from __future__ import annotations

import subprocess
import sys

from rich.console import Console
from rich.panel import Panel

console = Console()


def _do_uninstall() -> None:
    """Remove open-tulid from the Python environment."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "open-tulid", "-y"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            console.print(Panel(
                "Failed to uninstall open-tulid.\n\n" + result.stderr.strip(),
                style="red",
            ))
            raise SystemExit(1)

        console.print(Panel("open-tulid uninstalled successfully.", style="green"))
    except FileNotFoundError:
        console.print(Panel(
            "pip is not available. Try running:\n\n  pip uninstall open-tulid",
            style="red",
        ))
        raise SystemExit(1)
