from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import typer

from project_analyzer import cli


def test_run_dispatches_to_subcommand_app(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "app", lambda: called.append("app"))
    monkeypatch.setattr(sys, "argv", ["panalyzer", "start"])

    cli.run()

    assert called == ["app"]


def test_restart_command_uses_runtime_restart(monkeypatch) -> None:
    outputs: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda value, err=False: outputs.append(value))
    monkeypatch.setattr(
        "project_analyzer.cli.WebAppRuntime.restart",
        lambda self: SimpleNamespace(running=True, host="127.0.0.1", port=7000),
    )

    cli.restart()

    assert outputs == ["Web app running on 127.0.0.1:7000"]


def test_run_prints_help(monkeypatch) -> None:
    outputs: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda value, err=False: outputs.append(value))
    monkeypatch.setattr(sys, "argv", ["panalyzer", "--help"])

    cli.run()

    assert outputs[0].startswith("Usage:")
    assert "panalyzer analyze <path>" in outputs[0]
    assert "panalyzer -a <path>" in outputs[0]
    assert "project endpoints" not in outputs[0]


def test_run_without_args_prints_help_and_exits(monkeypatch) -> None:
    outputs: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda value, err=False: outputs.append(value))
    monkeypatch.setattr(sys, "argv", ["panalyzer"])

    with pytest.raises(typer.Exit) as exc_info:
        cli.run()

    assert exc_info.value.exit_code == 0
    assert outputs[0].startswith("Usage:")


def test_run_legacy_path_emits_diagram_json(monkeypatch, tmp_path: Path) -> None:
    outputs: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda value, err=False: outputs.append(value))
    monkeypatch.setattr(sys, "argv", ["panalyzer", str(tmp_path)])

    monkeypatch.setattr(
        "project_analyzer.cli.ProjectAnalysisService.analyze_project",
        lambda self, path: SimpleNamespace(
            diagram=SimpleNamespace(model_dump_json=lambda indent=2: json.dumps({"kind": "diagram", "root": str(path)})),
            graph=SimpleNamespace(model_dump_json=lambda indent=2: json.dumps({"kind": "graph", "root": str(path)})),
        ),
    )

    cli.run()

    assert json.loads(outputs[0]) == {"kind": "diagram", "root": str(tmp_path.resolve())}


def test_run_legacy_full_emits_graph_json(monkeypatch, tmp_path: Path) -> None:
    outputs: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda value, err=False: outputs.append(value))
    monkeypatch.setattr(sys, "argv", ["panalyzer", "-a", str(tmp_path)])

    monkeypatch.setattr(
        "project_analyzer.cli.ProjectAnalysisService.analyze_project",
        lambda self, path: SimpleNamespace(
            diagram=SimpleNamespace(model_dump_json=lambda indent=2: json.dumps({"kind": "diagram"})),
            graph=SimpleNamespace(model_dump_json=lambda indent=2: json.dumps({"kind": "graph", "root": str(path)})),
        ),
    )

    cli.run()

    assert json.loads(outputs[0]) == {"kind": "graph", "root": str(tmp_path.resolve())}


def test_analyze_command_can_write_output(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "out" / "scan.json"
    monkeypatch.setattr(
        "project_analyzer.cli.ProjectAnalysisService.analyze_project",
        lambda self, path: SimpleNamespace(
            diagram=SimpleNamespace(model_dump_json=lambda indent=2: json.dumps({"kind": "diagram"})),
            graph=SimpleNamespace(model_dump_json=lambda indent=2: json.dumps({"kind": "graph"})),
        ),
    )
    outputs: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda value, err=False: outputs.append(value))

    cli.analyze_command(tmp_path, full=True, output=output_path)

    assert json.loads(output_path.read_text(encoding="utf-8")) == {"kind": "graph"}
    assert outputs == [str(output_path)]


def test_run_rejects_missing_legacy_path(monkeypatch) -> None:
    outputs: list[tuple[str, bool]] = []
    monkeypatch.setattr(cli.typer, "echo", lambda value, err=False: outputs.append((value, err)))
    monkeypatch.setattr(sys, "argv", ["panalyzer", "-a"])

    with pytest.raises(typer.Exit) as exc_info:
        cli.run()

    assert exc_info.value.exit_code == 2
    assert outputs[0][1] is True
    assert "expected exactly one project path" in outputs[0][0]
