from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType

import pytest

from open_tulid.runtime.execution_contracts import FrozenContextFile
from open_tulid.runtime.planning_inputs import lint_planning_inputs


def _file(content: str, *, required: bool = True, path: str = "context/spec.md"):
    return FrozenContextFile(
        workspace_path=path,
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        byte_count=len(content.encode()),
        required=required,
        reason="Defines behavior.",
        role="reference",
        refs=("artifacts/task/spec.md",),
    )


def _prompt(*, reads: str, body: str = "Plan the work.") -> str:
    return (
        "## Task Body\n\n" + body + "\n\n"
        "## Frozen source reading\nRead the complete required files.\n" + reads + "\n"
        "## Budget\n"
    )


def test_lint_planning_clean_packet_has_no_issues():
    content = "# Decisions\nThe answer is 42.\n"
    file = _file(content)
    reads = f"- Required: {file.workspace_path} (refs). reason.\n"
    errors = lint_planning_inputs(_prompt(reads=reads), (file,), source_task_body="Plan the work.")
    assert errors == ()


def test_lint_planning_reports_missing_required_source():
    file = _file("# Decisions\n42\n")
    errors = lint_planning_inputs(_prompt(reads=""), (file,), source_task_body="Plan the work.")
    assert any(error.code == "prompt.missing_required_source" for error in errors)


def test_lint_planning_reports_unresolved_reading_path():
    file = _file("# Decisions\n42\n", path="context/spec.md")
    errors = lint_planning_inputs(_prompt(reads="- Required: context/other.md (refs).\n"), (file,), source_task_body="")
    assert any(error.code == "prompt.unresolved_reading_path" for error in errors)


def test_lint_planning_reports_saved_byte_hash_mismatch():
    file = _file("# Decisions\n42\n")
    # Tamper the stored content without updating the digest.
    tampered = FrozenContextFile(
        workspace_path=file.workspace_path,
        content="# Decisions\n43\n",
        sha256=file.sha256,
        byte_count=file.byte_count,
        required=True,
        reason=file.reason,
        role=file.role,
        refs=file.refs,
    )
    reads = f"- Required: {file.workspace_path} (refs). reason.\n"
    errors = lint_planning_inputs(_prompt(reads=reads), (tampered,), source_task_body="")
    assert any(error.code == "prompt.context_hash_mismatch" for error in errors)


def test_lint_planning_reports_duplicate_authoritative_task_body():
    file = _file("# Decisions\n42\n", required=False)
    body = "Plan the exact behavior exactly once."
    reads = f"- Background: {file.workspace_path} (refs). reason.\n"
    prompt = _prompt(reads=reads, body=body) + "\n" + body
    errors = lint_planning_inputs(prompt, (file,), source_task_body=body)
    assert any(error.code == "prompt.duplicate_task_content" for error in errors)


def test_lint_planning_optional_background_may_be_omitted():
    # Optional context that is not named in reading is allowed to be omitted.
    optional = _file("# Optional\ncontent.\n", required=False, path="context/optional.md")
    required = _file("# Required\n42\n", required=True, path="context/spec.md")
    reads = f"- Required: {required.workspace_path} (refs). reason.\n"
    errors = lint_planning_inputs(_prompt(reads=reads), (optional, required), source_task_body="")
    assert all(error.code != "prompt.missing_required_source" for error in errors)
