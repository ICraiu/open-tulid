from __future__ import annotations

import json
import subprocess
from pathlib import Path

from open_tulid.domain import EventType
from open_tulid.runtime import JsonlEventStore, OperationEventLogger
from open_tulid.workflow import get_builtin_registries
from open_tulid.workflow.implementations import (
    WorkflowExecutionContext,
    artifact_has_required_text,
    artifacts_match_template,
    copy_file,
    file_exists,
    git_reset_hard,
    implementation_contract_valid,
    template_required_fields_present,
    template_sections_present,
    write_file,
)


class TestBuiltinImplementations:
    def test_all_builtin_validations_are_callable(self):
        regs = get_builtin_registries()
        assert all(callable(spec.implementation) for spec in regs.validations.values())

    def test_all_builtin_operations_are_callable(self):
        regs = get_builtin_registries()
        assert all(callable(spec.implementation) for spec in regs.operations.values())


class TestValidationImplementations:
    def test_file_exists_passes_for_existing_project_relative_file(self, tmp_path: Path):
        (tmp_path / "artifact.md").write_text("ok", encoding="utf-8")
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = file_exists(ctx, path="artifact.md")

        assert result.passed is True
        assert result.code == "file_exists"

    def test_template_sections_present_reports_missing_section(self, tmp_path: Path):
        path = tmp_path / "artifact.md"
        path.write_text("## Summary\n\nDone\n", encoding="utf-8")
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = template_sections_present(ctx, path="artifact.md", sections=["Summary", "Proof"])

        assert result.passed is False
        assert "Proof" in result.message

    def test_template_required_fields_present_checks_non_empty_field(self):
        ctx = WorkflowExecutionContext()

        result = template_required_fields_present(
            ctx,
            content="## Proof\n\nEvidence: yes\nChanged files:\n",
            fields=["Evidence", "Changed files"],
        )

        assert result.passed is False
        assert "Changed files" in result.message

    def test_artifact_has_required_text_can_check_substring(self):
        ctx = WorkflowExecutionContext()

        result = artifact_has_required_text(ctx, content="build passed", text="passed")

        assert result.passed is True

    def test_artifacts_match_template_checks_all_matching_files(self, tmp_path: Path):
        output = tmp_path / "output"
        output.mkdir()
        (output / "ok.md").write_text("## Purpose\n\nDone\n\nField: yes\n", encoding="utf-8")
        (output / "bad.md").write_text("## Purpose\n\nDone\n", encoding="utf-8")
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = artifacts_match_template(
            ctx,
            pattern="output/*.md",
            sections=["Purpose"],
            fields=["Field"],
        )

        assert result.passed is False
        assert "bad.md missing fields: Field" in result.message

    def test_artifacts_match_template_passes_when_all_files_match(self, tmp_path: Path):
        output = tmp_path / "output"
        output.mkdir()
        (output / "one.md").write_text("## Purpose\n\nDone\n\nField: yes\n", encoding="utf-8")
        (output / "two.md").write_text("## Purpose\n\nDone\n\nField: yes\n", encoding="utf-8")
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = artifacts_match_template(
            ctx,
            pattern="output/*.md",
            sections=["Purpose"],
            fields=["Field"],
        )

        assert result.passed is True

    def test_implementation_contract_valid_binds_artifact_to_job_task(self, tmp_path: Path):
        context_dir = tmp_path / ".open-tulid"
        context_dir.mkdir()
        context_dir.joinpath("job-context.json").write_text(
            json.dumps({
                "source_intent_sha256": "a" * 64,
                "task": {"id": "task-1"},
            }),
            encoding="utf-8",
        )
        output = tmp_path / "output"
        output.mkdir()
        output.joinpath("implementation-contract.yaml").write_text(
            """\
schema: tulid.implementation/v1
source:
  task_id: task-1
  source_intent_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
profile: code_change
objective: Make health observable.
change_surface:
  add: []
  edit: [app.py]
  forbidden: []
requirements: [The endpoint returns ok.]
checks:
  focused:
    - id: health
      argv: [python, check_repo.py, tests]
  invariants: []
""",
            encoding="utf-8",
        )
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = implementation_contract_valid(
            ctx,
            path="output/implementation-contract.yaml",
        )

        assert result.passed is True
        assert result.data["profile"] == "code_change"

    def test_implementation_contract_valid_reports_stale_hash(self, tmp_path: Path):
        context_dir = tmp_path / ".open-tulid"
        context_dir.mkdir()
        context_dir.joinpath("job-context.json").write_text(
            json.dumps({
                "source_intent_sha256": "b" * 64,
                "task": {"id": "task-1"},
            }),
            encoding="utf-8",
        )
        output = tmp_path / "output"
        output.mkdir()
        output.joinpath("implementation-contract.yaml").write_text(
            """\
schema: tulid.implementation/v1
source:
  task_id: task-1
  source_intent_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
profile: code_change
objective: Make health observable.
change_surface:
  add: []
  edit: [app.py]
  forbidden: []
requirements: [The endpoint returns ok.]
checks:
  focused:
    - id: health
      argv: [python, check_repo.py, tests]
  invariants: []
""",
            encoding="utf-8",
        )
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = implementation_contract_valid(
            ctx,
            path="output/implementation-contract.yaml",
        )

        assert result.passed is False
        assert "contract.source_hash_mismatch" in result.data["error_codes"]

    def test_implementation_contract_valid_rejects_workspace_escape(self, tmp_path: Path):
        ctx = WorkflowExecutionContext(project_root=tmp_path / "workspace")

        result = implementation_contract_valid(
            ctx,
            path="../implementation-contract.yaml",
        )

        assert result.passed is False
        assert result.data["error_codes"] == ["contract.path_escape"]


class TestOperationImplementations:
    def test_write_file_creates_parent_directories(self, tmp_path: Path):
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = write_file(ctx, path="nested/out.txt", content="hello")

        assert result.accepted is True
        assert (tmp_path / "nested" / "out.txt").read_text(encoding="utf-8") == "hello"

    def test_copy_file_copies_bytes(self, tmp_path: Path):
        source = tmp_path / "source.txt"
        source.write_text("hello", encoding="utf-8")
        ctx = WorkflowExecutionContext(project_root=tmp_path)

        result = copy_file(ctx, source="source.txt", target="target.txt")

        assert result.accepted is True
        assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "hello"

    def test_git_reset_hard_requires_approval(self, tmp_path: Path):
        calls: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        ctx = WorkflowExecutionContext(project_root=tmp_path, command_runner=runner)

        result = git_reset_hard(ctx)

        assert result.accepted is False
        assert calls == []

    def test_git_reset_hard_runs_when_approved(self, tmp_path: Path):
        calls: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        ctx = WorkflowExecutionContext(
            project_root=tmp_path,
            command_runner=runner,
            approved_destructive=True,
        )

        result = git_reset_hard(ctx, target="HEAD~1")

        assert result.accepted is True
        assert calls == [("git", "reset", "--hard", "HEAD~1")]

    def test_registry_operation_logs_started_and_finished_events(self, tmp_path: Path):
        store = JsonlEventStore(tmp_path / "events")
        ctx = WorkflowExecutionContext(
            project_id="Agent",
            project_root=tmp_path,
            actor_type="cli",
            actor_id="test-user",
            correlation_id="01J00000000000000000000COR",
            operation_logger=OperationEventLogger(store),
        )
        operation = get_builtin_registries().operations["write_file"].implementation

        result = operation(ctx, path="out.txt", content="hello")

        assert result.accepted is True
        events = store.iter_events()
        assert [event.event_type for event in events] == [
            EventType.OperationStarted.value,
            EventType.OperationFinished.value,
        ]
        assert events[0].data["operation"] == "write_file"
        assert events[1].data["accepted"] is True

    def test_registry_operation_logs_failed_events(self, tmp_path: Path):
        store = JsonlEventStore(tmp_path / "events")
        ctx = WorkflowExecutionContext(
            project_id="Agent",
            project_root=tmp_path,
            actor_type="cli",
            actor_id="test-user",
            correlation_id="01J00000000000000000000COR",
            operation_logger=OperationEventLogger(store),
        )
        operation = get_builtin_registries().operations["copy_file"].implementation

        result = operation(ctx, source="missing.txt", target="out.txt")

        assert result.accepted is False
        events = store.iter_events()
        assert [event.event_type for event in events] == [
            EventType.OperationStarted.value,
            EventType.OperationFailed.value,
        ]
        assert events[1].data["operation"] == "copy_file"
