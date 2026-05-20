from __future__ import annotations

from pathlib import Path

from open_tulid.domain import Task
from open_tulid.runtime.context import LinkedContextResolver


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
