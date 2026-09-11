"""Durable inputs for artifact-producing jobs, without a code-change contract."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Mapping
from open_tulid.domain import DomainError, Task, TransitionDefinition

from .execution_contracts import (
    FrozenContextFile, _task_from_dict, _task_to_dict,
    _transition_from_dict, _transition_to_dict,
)
from .repository_facts import (
    SourceSelection,
    canonical_sha256,
    source_selection_from_dict,
    source_selection_to_dict,
)

SCHEMA = "tulid.planning-inputs/v1"
REPOSITORY_BASELINE_SCHEMA = "tulid.planning-repository-baseline/v1"
INLINE_CHARACTER_LIMIT = 64_000


@dataclass(frozen=True)
class FrozenRepositoryBaseline:
    """The frozen source baseline a planning job was admitted against.

    ``PlanningInputs`` freeze the task, transition, context, and prompt, but a
    planner also needs the same repository baseline that is actually supplied
    into the workspace at launch. This record binds the frozen source-selection
    rule (so a workspace without ``.git`` applies the identical rule) to the
    admitted baseline manifest digest and base commit. At preparation, if the
    live repository differs from this frozen baseline, preparation must block
    with a stale-baseline diagnostic rather than silently substituting the new
    repository into an admitted planning packet.

    ``None`` on a planning job means the job was admitted without a resolved
    source repository (artifact-only planning) or is a historical v1 record;
    such jobs retain their historical interpretation and acquire no code-diff /
    baseline requirement.
    """

    schema: str
    repository_available: bool
    git_repository: bool
    base_commit: str | None
    baseline_manifest_sha256: str
    source_selection: SourceSelection | None


@dataclass(frozen=True)
class PlanningInputs:
    source_task: Task
    transition: TransitionDefinition
    context_files: tuple[FrozenContextFile, ...]
    prompt: str
    repository_baseline: FrozenRepositoryBaseline | None = None

    @property
    def source_identities(self):
        return tuple(sorted({(ref, file.sha256) for file in self.context_files
                             if file.required for ref in file.refs}))


def freeze_planning_inputs(
    task,
    transition,
    prompt,
    context_files,
    repository_baseline: FrozenRepositoryBaseline | None = None,
):
    body = {
        "schema": SCHEMA,
        "source_task": _task_to_dict(task),
        "transition": _transition_to_dict(transition),
        "context_files": [asdict(file) for file in context_files],
        "prompt": prompt,
        "inline_character_limit": INLINE_CHARACTER_LIMIT,
        "repository_baseline": (
            _repository_baseline_to_dict(repository_baseline)
            if repository_baseline is not None
            else None
        ),
    }
    return {**body, "sha256": canonical_sha256(body)}


def _repository_baseline_to_dict(baseline: FrozenRepositoryBaseline) -> dict[str, object]:
    return {
        "schema": baseline.schema,
        "repository_available": baseline.repository_available,
        "git_repository": baseline.git_repository,
        "base_commit": baseline.base_commit,
        "baseline_manifest_sha256": baseline.baseline_manifest_sha256,
        "source_selection": (
            source_selection_to_dict(baseline.source_selection)
            if baseline.source_selection is not None
            else None
        ),
    }


def _repository_baseline_from_dict(payload: Mapping[str, object]) -> FrozenRepositoryBaseline:
    selection_payload = payload.get("source_selection")
    selection = (
        source_selection_from_dict(selection_payload)
        if isinstance(selection_payload, Mapping)
        else None
    )
    return FrozenRepositoryBaseline(
        schema=str(payload.get("schema", REPOSITORY_BASELINE_SCHEMA)),
        repository_available=bool(payload.get("repository_available", False)),
        git_repository=bool(payload.get("git_repository", False)),
        base_commit=(
            str(payload["base_commit"])
            if payload.get("base_commit") is not None else None
        ),
        baseline_manifest_sha256=str(payload.get("baseline_manifest_sha256", "")),
        source_selection=selection,
    )


def load_planning_inputs(job) -> PlanningInputs | None:
    raw = job.metadata.get("planning_inputs")
    if raw is None:
        return None  # Historical jobs retain their legacy interpretation.
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ValueError("unsupported frozen planning inputs")
    body = {key: value for key, value in raw.items() if key != "sha256"}
    if canonical_sha256(body) != raw.get("sha256"):
        raise ValueError("frozen planning inputs digest mismatch")
    task = _task_from_dict(raw["source_task"])
    transition = _transition_from_dict(raw["transition"])
    if (task.id, transition.id, transition.worker) != (job.task_id, job.transition_id, job.worker_id):
        raise ValueError("frozen planning inputs do not match the job identity")
    files = tuple(FrozenContextFile(**file) for file in raw["context_files"])
    for file in files:
        content = file.content.encode("utf-8")
        if len(content) != file.byte_count or hashlib.sha256(content).hexdigest() != file.sha256:
            raise ValueError("frozen planning reference digest mismatch")
    prompt = raw["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("frozen planning prompt is missing")
    baseline_raw = raw.get("repository_baseline")
    baseline = (
        _repository_baseline_from_dict(baseline_raw)
        if isinstance(baseline_raw, Mapping)
        else None
    )
    return PlanningInputs(task, transition, files, prompt, repository_baseline=baseline)


def frozen_repository_baseline_from_snapshot(
    snapshot: object,
) -> FrozenRepositoryBaseline | None:
    """Build a frozen planning baseline from a ``RepositorySnapshotResult``.

    Returns ``None`` when there is no resolved source repository (artifact-only
    planning) so such planning acquires no source-diff requirement. Errors are
    treated as no baseline: resolution failure is a preparation error already
    surfaced by the launch path itself.
    """
    from .repository_facts import RepositorySnapshotResult

    if snapshot is None or not isinstance(snapshot, RepositorySnapshotResult):
        return None
    if not snapshot.accepted or snapshot.snapshot is None:
        return None
    facts = snapshot.snapshot.facts
    return FrozenRepositoryBaseline(
        schema=REPOSITORY_BASELINE_SCHEMA,
        repository_available=facts.repository_available,
        git_repository=facts.git_repository,
        base_commit=facts.base_commit,
        baseline_manifest_sha256=snapshot.snapshot.baseline.sha256,
        source_selection=facts.source_selection,
    )


def lint_planning_inputs(
    prompt: str,
    context_files,
    source_task_body: str,
    *,
    job_id: str = "planning",
) -> tuple[DomainError, ...]:
    """Structural integrity checks for a planning (rendered or frozen) packet.

    Mirrors the implementation-contract lint for the planning route: every
    required frozen source must be named in required reading; every named reading
    path must resolve to a frozen file; saved bytes must match their declared
    digest; and the authoritative task body must not be duplicated. Optional
    background may be omitted, but required sources cannot.
    """
    from open_tulid.domain import DomainError

    errors: list[DomainError] = []
    reading_section = _reading_section(prompt)
    named_paths = _reading_context_paths(reading_section)
    frozen_paths = {file.workspace_path for file in context_files}
    missing = tuple(
        file.workspace_path
        for file in context_files
        if file.required and file.workspace_path not in named_paths
    )
    for path in missing:
        errors.append(DomainError(
            "prompt.missing_required_source",
            f"Required reading omits frozen source file `{path}`.",
            path,
        ))
    for ref in sorted(named_paths - frozen_paths):
        errors.append(DomainError(
            "prompt.unresolved_reading_path",
            f"Required reading names workspace file `{ref}` with no matching frozen source.",
            ref,
        ))
    for file in context_files:
        content = file.content.encode("utf-8")
        if len(content) != file.byte_count or hashlib.sha256(content).hexdigest() != file.sha256:
            errors.append(DomainError(
                "prompt.context_hash_mismatch",
                f"Frozen planning reference {file.workspace_path!r} failed its byte/hash integrity check.",
                file.workspace_path,
            ))
    body = source_task_body.strip()
    if body and prompt.count(body) > 1:
        errors.append(DomainError(
            "prompt.duplicate_task_content",
            "Authoritative task body appears more than once in the planning packet.",
            "assigned_task",
        ))
    return tuple(errors)


def _reading_section(prompt: str) -> str:
    marker = "## Frozen source reading"
    if marker not in prompt:
        return ""
    tail = prompt.split(marker, 1)[1]
    # Reading section ends at the next top-level section heading.
    for line in tail.splitlines():
        if line.startswith("## ") and "Frozen source reading" not in line:
            return tail
    return tail


def _reading_context_paths(reading: str) -> set[str]:
    import re as _re
    return {
        path
        for path in _re.findall(r"(?:Required|Background):\s+([^\s(]+)", reading)
        if path
    }
