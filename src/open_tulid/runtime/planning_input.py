"""Planning input synthesis for specification and breakdown transitions.

Before the spec and breakdown workers generate artifacts, they need repository
facts and the current unfinished task set, not just the linked document context.
This module produces that deterministic planning input text so a worker can
inspect actual manifests and seams, distinguish implemented behavior from
remaining work, account for existing unfinished work, and avoid duplicate
ownership when it publishes new tasks.

Only the planning transitions that produce an ``ImplementationSpec`` (spec run)
or derive ``ImplementationTask`` files (breakdown run) receive these inputs.
Linked files are not recursively imported from every unfinished task; each
unfinished task is supplied as its own body with identity and dependency edges.
"""
from __future__ import annotations

from dataclasses import dataclass

from open_tulid.adapters.base import LoadProjectResult
from open_tulid.domain import Task, TransitionDefinition
from open_tulid.runtime.repository_facts import RepositorySnapshotResult


@dataclass(frozen=True)
class PlanningInputs:
    """Synthesized planning inputs for one specification/breakdown transition."""

    repository_facts: str
    unfinished_tasks: tuple[Task, ...]
    text: str


def requires_planning_inputs(transition: TransitionDefinition) -> bool:
    """True for the specification and breakdown planning transitions only."""
    if transition.derives is not None and transition.derives.task_type == "ImplementationTask":
        return True
    return any(artifact == "ImplementationSpec" for artifact in transition.requires.artifacts)


def build_planning_inputs(
    *,
    repository: RepositorySnapshotResult | None,
    project: LoadProjectResult | None,
    current_task: Task,
    parent_tasks: tuple[Task, ...] = (),
) -> PlanningInputs:
    """Build the planning input text from the repository and project snapshots.

    The current task and its parent lineage are excluded from the unfinished
    task inventory because they are already supplied as authoritative context
    elsewhere and must not be duplicated here.
    """
    repository_text = _render_repository_facts(repository)
    unfinished = _unfinished_tasks_in_scope(project, current_task, parent_tasks)
    unfinished_text = _render_unfinished_tasks(unfinished)

    sections: list[str] = []
    if repository_text:
        sections.append(repository_text)
    if unfinished_text:
        sections.append(unfinished_text)
    if not sections:
        return PlanningInputs(repository_facts="", unfinished_tasks=unfinished, text="")

    heading = (
        "# Planning Inputs\n"
        "\n"
        "These inputs describe the current repository and the existing unfinished "
        "task set before you produce new planning artifacts. Inspect the named "
        "manifests and integration seams in the workspace; they are guidance about "
        "responsibility, not a file allowlist or a per-task command contract.\n"
    )
    return PlanningInputs(
        repository_facts=repository_text,
        unfinished_tasks=unfinished,
        text=f"{heading}\n\n" + "\n\n".join(sections),
    )


def _unfinished_tasks_in_scope(
    project: LoadProjectResult | None,
    current_task: Task,
    parent_tasks: tuple[Task, ...],
) -> tuple[Task, ...]:
    if project is None or project.snapshot is None:
        return ()
    excluded = {current_task.id}
    excluded.update(parent.id for parent in parent_tasks)
    return tuple(
        sorted(
            (
                task
                for task in project.snapshot.tasks.values()
                if task.id not in excluded and task.current_state != "Done"
            ),
            key=lambda task: task.id,
        )
    )


def _render_repository_facts(repository: RepositorySnapshotResult | None) -> str:
    if repository is None or not repository.accepted or repository.snapshot is None:
        return ""
    facts = repository.snapshot.facts
    dirty = facts.dirty if facts.dirty is not None else "unknown"
    lines = [
        "## Repository Facts",
        "Repository is available." if facts.repository_available else "No repository is available.",
        "Top-level: " + ", ".join(facts.top_level_entries),
        "Manifests: " + ", ".join(facts.manifests),
        "Entrypoints: " + ", ".join(facts.detected_entrypoints),
        f"Base commit: {facts.base_commit or 'none'} (dirty: {dirty})",
        "",
        "Inspect the actual manifests and integration seams you need before planning. "
        "Distinguish behavior already implemented from remaining work, preserve "
        "interfaces already promised by earlier tasks, and never let a task instruct "
        "an implementation worker to select architecture already fixed by this work.",
    ]
    return "\n".join(lines)


def _render_unfinished_tasks(tasks: tuple[Task, ...]) -> str:
    if not tasks:
        return ""
    lines = [
        "## Unfinished Project Tasks",
        "",
        "The following tasks in this project are not yet Done. Treat them as existing "
        "agreed work that will be executed or reviewed: account for them, reuse or "
        "respect the interfaces they already promise, and never regenerate or "
        "duplicate a task another artifact already owns. Each entry is this task's "
        "own body; their linked files are not imported here.",
        "",
    ]
    for task in tasks:
        dependencies = ", ".join(task.dependencies) if task.dependencies else "none"
        lines.append(f"### {task.id} — {task.title}")
        lines.append(f"State: {task.current_state}")
        lines.append(f"Type: {task.task_type}")
        lines.append(f"Dependencies: {dependencies}")
        if task.body.strip():
            lines.append("")
            lines.append(task.body.strip())
        lines.append("")
    return "\n".join(lines).rstrip()
