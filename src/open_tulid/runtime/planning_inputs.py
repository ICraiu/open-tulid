"""Durable inputs for artifact-producing jobs, without a code-change contract."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Mapping
from open_tulid.domain import Task, TransitionDefinition

from .execution_contracts import (
    FrozenContextFile, _task_from_dict, _task_to_dict,
    _transition_from_dict, _transition_to_dict,
)
from .repository_facts import canonical_sha256

SCHEMA = "tulid.planning-inputs/v1"
INLINE_CHARACTER_LIMIT = 64_000


@dataclass(frozen=True)
class PlanningInputs:
    source_task: Task
    transition: TransitionDefinition
    context_files: tuple[FrozenContextFile, ...]
    prompt: str

    @property
    def source_identities(self):
        return tuple(sorted({(ref, file.sha256) for file in self.context_files
                             if file.required for ref in file.refs}))


def freeze_planning_inputs(task, transition, prompt, context_files):
    body = {
        "schema": SCHEMA,
        "source_task": _task_to_dict(task),
        "transition": _transition_to_dict(transition),
        "context_files": [asdict(file) for file in context_files],
        "prompt": prompt,
        "inline_character_limit": INLINE_CHARACTER_LIMIT,
    }
    return {**body, "sha256": canonical_sha256(body)}


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
    return PlanningInputs(task, transition, files, prompt)
