from __future__ import annotations

from pathlib import Path
import sys

import typer

from .runtime import WebAppRuntime
from .services.project_analysis import ProjectAnalysisService


app = typer.Typer(
    add_completion=False,
    help="Control the panalyzer web app and export project structure.",
)


@app.command("analyze")
def analyze_command(
    path: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    full: bool = typer.Option(False, "--full", "-a", help="Emit the full method/reference graph JSON."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON to a file instead of stdout."),
) -> None:
    """Analyze a local project and emit JSON."""

    _emit_analysis(path, full=full, output=output)


@app.command("export")
def export_command(
    path: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    full: bool = typer.Option(False, "--full", "-a", help="Emit the full method/reference graph JSON."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON to a file instead of stdout."),
) -> None:
    """Alias for analyze."""

    _emit_analysis(path, full=full, output=output)


@app.command()
def start() -> None:
    """Start the panalyzer web app."""

    runtime = WebAppRuntime(Path.cwd())
    status = runtime.start()
    state = "running" if status.running else "stopped"
    typer.echo(f"Web app {state} on {status.host}:{status.port}")


@app.command()
def stop() -> None:
    """Stop the panalyzer web app."""

    runtime = WebAppRuntime(Path.cwd())
    status = runtime.stop()
    typer.echo(f"Web app stopped. Last known port: {status.port}")


@app.command()
def restart() -> None:
    """Restart the panalyzer web app, or start it if stopped."""

    runtime = WebAppRuntime(Path.cwd())
    status = runtime.restart()
    state = "running" if status.running else "stopped"
    typer.echo(f"Web app {state} on {status.host}:{status.port}")


@app.command()
def status() -> None:
    """Report whether the panalyzer web app is running."""

    runtime = WebAppRuntime(Path.cwd())
    current = runtime.status()
    if current.running:
        typer.echo(f"running pid={current.pid} host={current.host} port={current.port}")
        return
    typer.echo(f"stopped host={current.host} port={current.port}")


def run() -> None:
    """Console entrypoint for the installed command."""

    args = sys.argv[1:]
    if not args:
        typer.echo(_help_text())
        raise typer.Exit(0)

    first = args[0]
    if first in {"start", "stop", "restart", "status", "analyze", "export"}:
        app()
        return

    if first in {"--help", "-h", "help"}:
        typer.echo(_help_text())
        return

    _run_legacy_analysis(args)


def _run_legacy_analysis(args: list[str]) -> None:
    full = False
    output: Path | None = None
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--full", "-a"}:
            full = True
            index += 1
            continue
        if arg in {"--output", "-o"}:
            if index + 1 >= len(args):
                typer.echo("Error: --output requires a file path.", err=True)
                raise typer.Exit(2)
            output = Path(args[index + 1])
            index += 2
            continue
        if arg.startswith("-"):
            typer.echo(f"Error: unknown option {arg}", err=True)
            raise typer.Exit(2)
        remaining.append(arg)
        index += 1

    if len(remaining) != 1:
        typer.echo("Error: expected exactly one project path.", err=True)
        raise typer.Exit(2)

    path = Path(remaining[0]).expanduser()
    if not path.exists() or not path.is_dir():
        typer.echo(f"Error: project path is not a readable directory: {path}", err=True)
        raise typer.Exit(2)

    _emit_analysis(path.resolve(), full=full, output=output)


def _emit_analysis(path: Path, *, full: bool, output: Path | None) -> None:
    artifacts = ProjectAnalysisService().analyze_project(path)
    document = artifacts.graph if full else artifacts.diagram
    payload = document.model_dump_json(indent=2)
    if output is None:
        typer.echo(payload)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload + "\n", encoding="utf-8")
    typer.echo(str(output))


def _help_text() -> str:
    return """Usage:
  panalyzer analyze <path>
  panalyzer analyze --full <path>
  panalyzer export <path>
  panalyzer <path>
  panalyzer -a <path>
  panalyzer start
  panalyzer stop
  panalyzer restart
  panalyzer status

Commands:
  analyze  Analyze a local project and emit JSON.
  export   Alias for analyze.
  start    Start the panalyzer web app.
  stop     Stop the panalyzer web app.
  restart  Restart the panalyzer web app, or start it if stopped.
  status   Report whether the panalyzer web app is running.

Options:
  --full, -a       Emit the full method/reference graph JSON.
  --output, -o     Write JSON to a file instead of stdout.

Notes:
  Default analysis emits the package/file transition diagram.
  Full analysis emits package/file/method nodes and call edges.
"""


if __name__ == "__main__":
    run()
