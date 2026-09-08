from __future__ import annotations

from pathlib import Path

from open_tulid.domain import (
    DerivesDefinition,
    RequirementDefinition,
    Task,
    TransitionDefinition,
)
from open_tulid.runtime.context import (
    LinkedContextResolver,
    load_parent_tasks,
    task_for_context,
)


def _task(*, body: str = "", artifact_links: tuple[str, ...] = (), task_id: str = "01J00000000000000000000001") -> Task:
    return Task(
        id=task_id,
        title="Task",
        path="tasks/task.md",
        current_state="Todo",
        artifact_links=artifact_links,
        body=body,
    )


def test_linked_context_includes_artifacts_and_recursive_wiki_links(tmp_path: Path):
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "artifacts" / "spec.md").write_text("Spec body. See [[follow-up]].\n", encoding="utf-8")
    (tmp_path / "docs" / "follow-up.md").write_text("Follow-up body.\n", encoding="utf-8")

    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(artifact_links=("artifacts/spec.md",)),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert [doc.ref for doc in result.packet.documents] == ["artifacts/spec.md", "follow-up"]
    assert "Spec body" in result.packet.text
    assert "Follow-up body" in result.packet.text


def test_linked_context_dedupes_cycles(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("A -> [[b]]\n", encoding="utf-8")
    (tmp_path / "docs" / "b.md").write_text("B -> [[a]]\n", encoding="utf-8")

    result = LinkedContextResolver(tmp_path).build_context_packet(_task(body="See [[a]]."))

    assert result.accepted is True
    assert result.packet is not None
    assert [doc.ref for doc in result.packet.documents] == ["a", "b"]


def test_linked_context_rejects_missing_required_artifact_link(tmp_path: Path):
    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(artifact_links=("artifacts/missing.md",)),
    )

    assert result.accepted is False
    assert result.errors[0].code == "context.link_not_found"


def test_linked_context_rejects_artifact_path_escape(tmp_path: Path):
    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(artifact_links=("../secret.md",)),
    )

    assert result.accepted is False
    assert result.errors[0].code == "context.link_not_found"


def test_linked_context_includes_parent_links(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "parent-context.md").write_text("Parent context.\n", encoding="utf-8")

    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(body="Child body."),
        parent_tasks=(_task(body="See [[parent-context]].", task_id="parent"),),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert [doc.ref for doc in result.packet.documents] == ["parent-context"]


def test_question_round_rejects_conflicting_current_answer_artifacts(tmp_path: Path):
    result = LinkedContextResolver(tmp_path).build_context_packet(
        Task(
            id="question-round",
            title="Questions",
            path="tasks/questions.md",
            current_state="AnswersReady",
            task_type="QuestionRound",
            artifact_links=(
                "artifacts/2/QuestionRoundFile/initial.md",
                "artifacts/2/QuestionRoundFile/edited-copy.md",
            ),
        ),
    )

    assert result.accepted is False
    assert result.errors[0].code == "context.question_round_answer_conflict"


def test_linked_context_skips_parent_implementation_task_files(tmp_path: Path):
    (tmp_path / "artifacts" / "parent" / "ImplementationTaskFile").mkdir(parents=True)
    (tmp_path / "artifacts" / "parent" / "ImplementationSpec").mkdir(parents=True)
    task_file = "artifacts/parent/ImplementationTaskFile/01-project-shell.md"
    spec_file = "artifacts/parent/ImplementationSpec/implementation-spec.md"
    (tmp_path / task_file).write_text("Sibling task content.\n", encoding="utf-8")
    (tmp_path / spec_file).write_text("Implementation spec content.\n", encoding="utf-8")

    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(body="Child body."),
        parent_tasks=(_task(artifact_links=(task_file, spec_file), task_id="parent"),),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert [doc.ref for doc in result.packet.documents] == [spec_file]
    assert "Sibling task content" not in result.packet.text


def test_linked_context_ignores_generated_derived_task_section_in_task_bodies(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real-context.md").write_text("Real context.\n", encoding="utf-8")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "stale-child.md").write_text("Stale child.\n", encoding="utf-8")

    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(
            body="See [[real-context]].\n\n## Derived tasks\n- [[stale-child]]\n",
        ),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert [doc.ref for doc in result.packet.documents] == ["real-context"]
    assert "Stale child" not in result.packet.text


def test_linked_context_skips_direct_implementation_task_file_links(tmp_path: Path):
    (tmp_path / "artifacts" / "parent" / "ImplementationTaskFile").mkdir(parents=True)
    task_file = "artifacts/parent/ImplementationTaskFile/01-project-shell.md"
    (tmp_path / task_file).write_text("Task file content.\n", encoding="utf-8")

    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(artifact_links=(task_file,)),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert result.packet.documents == ()


def test_linked_context_dedupes_equal_content(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("Same content.\n", encoding="utf-8")
    (tmp_path / "docs" / "b.md").write_text("Same content.\n", encoding="utf-8")

    result = LinkedContextResolver(tmp_path).build_context_packet(_task(body="See [[a]] and [[b]]."))

    assert result.accepted is True
    assert result.packet is not None
    assert [doc.ref for doc in result.packet.documents] == ["a"]


def test_linked_context_marks_generated_execution_contract_as_binding(tmp_path: Path):
    contract_path = (
        tmp_path
        / "artifacts"
        / "task-1"
        / "ImplementationContract"
        / "implementation-contract.yaml"
    )
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(
        "schema: tulid.implementation/v1\nobjective: Add healthz.\n",
        encoding="utf-8",
    )
    ref = str(contract_path.relative_to(tmp_path))

    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(artifact_links=(ref,)),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert result.packet.documents[0].is_execution_contract is True
    assert f"Generated Execution Contract: {ref}" in result.packet.text
    assert "binding for implementation scope, interfaces, requirements, and checks" in result.packet.text


def test_linked_context_includes_only_latest_generated_contract_version(tmp_path: Path):
    contract_dir = tmp_path / "artifacts" / "task-1" / "ImplementationContract"
    contract_dir.mkdir(parents=True)
    old = contract_dir / "implementation-contract-old.yaml"
    current = contract_dir / "implementation-contract-current.yaml"
    old.write_text("objective: Old intent.\n", encoding="utf-8")
    current.write_text("objective: Current intent.\n", encoding="utf-8")
    old_ref = str(old.relative_to(tmp_path))
    current_ref = str(current.relative_to(tmp_path))

    result = LinkedContextResolver(tmp_path).build_context_packet(
        _task(artifact_links=(old_ref, current_ref)),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert [document.ref for document in result.packet.documents] == [current_ref]
    assert "Current intent" in result.packet.text
    assert "Old intent" not in result.packet.text


class _Adapter:
    def __init__(self, tasks: dict[str, Task]):
        self._tasks = tasks

    def read_task(self, task_id):
        from open_tulid.adapters.base import ReadTaskResult
        from open_tulid.domain import DomainError

        task = self._tasks.get(task_id)
        if task is None:
            return ReadTaskResult(errors=(DomainError("task.not_found", "missing"),))
        return ReadTaskResult(task=task)


def test_load_parent_tasks_stops_cleanly_at_a_missing_ancestor():
    root_id = "01J00000000000000000000000001"
    idea = Task(id=root_id, title="Idea", path="tasks/idea.md", current_state="Done", body="Original idea.")
    round_parent = Task(
        id="01J00000000000000000000000002",
        title="Clarify",
        path="tasks/clarify.md",
        current_state="Done",
        parent_id=root_id,
        body="A question round.",
    )
    child = Task(
        id="01J00000000000000000000000003",
        title="Implement",
        path="tasks/implement.md",
        current_state="Todo",
        parent_id=round_parent.id,
        body="Implement it.",
    )
    # The root idea is absent, so the walk returns the resolved portion only.
    adapter = _Adapter({round_parent.id: round_parent})
    parents = load_parent_tasks(adapter, child)
    assert [task.id for task in parents] == [round_parent.id]


def test_load_parent_tasks_presents_original_idea_first_then_rounds():
    root_id = "01J00000000000000000000000001"
    idea = Task(id=root_id, title="Idea", path="tasks/idea.md", current_state="Done", body="Original idea.")
    round_parent = Task(
        id="01J00000000000000000000000002",
        title="Clarify",
        path="tasks/clarify.md",
        current_state="Done",
        parent_id=root_id,
        body="A question round.",
    )
    child = Task(
        id="01J00000000000000000000000003",
        title="Implement",
        path="tasks/implement.md",
        current_state="Todo",
        parent_id=round_parent.id,
        body="Implement it.",
    )
    adapter = _Adapter({root_id: idea, round_parent.id: round_parent})
    parents = load_parent_tasks(adapter, child)
    assert [task.id for task in parents] == [root_id, round_parent.id]


def test_task_for_context_excludes_artifacts_the_transition_requires_or_derives():
    required_link = "artifacts/task-1/ImplementationTaskFile/deliverable.md"
    derived_link = "artifacts/task-1/PlanningTaskFile/plan.md"
    task = _task(artifact_links=(required_link, derived_link, "docs/spec.md"))
    transition = TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="Todo",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(
            changed_files_required=True,
            artifacts=("ImplementationTaskFile",),
        ),
        derives=DerivesDefinition(
            task_type="PlanningTask",
            state="Todo",
            artifact_type="PlanningTaskFile",
            required=True,
        ),
        transaction=None,
    )
    filtered = task_for_context(task, transition)
    assert required_link not in filtered.artifact_links
    assert derived_link not in filtered.artifact_links
    assert "docs/spec.md" in filtered.artifact_links
