from __future__ import annotations

from dataclasses import replace

from open_tulid.domain import Task
from open_tulid.runtime.task_contracts import (
    parse_implementation_contract,
    task_source_intent_sha256,
)


def _task() -> Task:
    return Task(
        id="task-1",
        title="Keep my task free-form",
        path="tasks/task-1.md",
        current_state="Todo",
        task_type="ImplementationTask",
        dependencies=("task-0",),
        artifact_links=("docs/original-spec.md",),
        parent_id="idea-1",
        metadata={"priority": "high", "custom": {"anything": True}},
        body="# Any shape is allowed\n\nPlease add the health endpoint.",
    )


def _contract(task: Task, *, profile: str = "code_change") -> str:
    return f"""\
schema: tulid.implementation/v1
source:
  task_id: {task.id}
  source_intent_sha256: {task_source_intent_sha256(task)}
profile: {profile}
objective: The health endpoint returns an observable ok response.
change_surface:
  add: []
  edit: [src/app.py]
  forbidden: [docs/product.md]
interfaces:
  - name: app.healthz
    behavior: Return the string ok.
requirements:
  - Preserve all existing endpoints.
failure_behavior: []
non_goals:
  - Do not change deployment configuration.
checks:
  focused:
    - id: health_test
      argv: [python, -m, pytest, tests/test_health.py, -q]
      timeout_seconds: 90
      expect:
        exit_code: 0
        stdout_contains: [passed]
  invariants: []
"""


def test_valid_contract_is_bound_to_arbitrarily_structured_task() -> None:
    task = _task()

    parsed = parse_implementation_contract(
        _contract(task),
        expected_task_id=task.id,
        expected_source_intent_sha256=task_source_intent_sha256(task),
    )

    assert parsed.accepted is True
    assert parsed.contract is not None
    assert parsed.contract.profile == "code_change"
    assert parsed.contract.change_surface.edit == ("src/app.py",)
    assert parsed.contract.focused_checks[0].argv == (
        "python",
        "-m",
        "pytest",
        "tests/test_health.py",
        "-q",
    )


def test_source_intent_hash_ignores_workflow_state_and_generated_links() -> None:
    task = _task()
    workflow_updated = replace(
        task,
        current_state="ReadyToImplement",
        artifact_links=(
            *task.artifact_links,
            "artifacts/task-1/ImplementationContract/implementation-contract.yaml",
        ),
    )

    assert task_source_intent_sha256(workflow_updated) == task_source_intent_sha256(task)


def test_source_intent_hash_changes_when_user_owned_body_changes() -> None:
    task = _task()

    assert task_source_intent_sha256(
        replace(task, body=f"{task.body}\n\nAlso preserve the metrics endpoint."),
    ) != task_source_intent_sha256(task)


def test_contract_rejects_stale_source_and_shell_control_tokens() -> None:
    task = _task()
    contract = _contract(task).replace(
        "argv: [python, -m, pytest, tests/test_health.py, -q]",
        "argv: [python, -m, pytest, '&&', echo]",
    )

    parsed = parse_implementation_contract(
        contract,
        expected_task_id=task.id,
        expected_source_intent_sha256="0" * 64,
    )

    assert parsed.accepted is False
    assert {error.code for error in parsed.errors} >= {
        "contract.source_hash_mismatch",
        "contract.check_shell_control",
    }


def test_documentation_profile_requires_markdown_change_surface() -> None:
    task = _task()

    parsed = parse_implementation_contract(
        _contract(task, profile="documentation"),
    )

    assert parsed.accepted is False
    assert "contract.documentation_surface_missing" in {
        error.code for error in parsed.errors
    }


def test_contract_rejects_unknown_fields_instead_of_silently_ignoring_them() -> None:
    task = _task()

    parsed = parse_implementation_contract(
        f"{_contract(task)}unexpected_policy: trust-me\n",
    )

    assert parsed.accepted is False
    assert "contract.unknown_field" in {error.code for error in parsed.errors}
