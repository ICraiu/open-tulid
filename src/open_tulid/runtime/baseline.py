"""Reproducible runtime/project baseline for an isolated worker fixture run.

Step 1A (reliability plan 1) requires recording the source revision and
dirty-patch identity, the installed Tulid location, the workflow hash, the
command-policy hash, the worker image identity, and sanitized effective worker
configuration in an isolated fixture run. The installed runtime and the source
checkout being edited are captured separately and never assumed to be the same.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from open_tulid.domain import WorkflowDefinition
from open_tulid.runtime.repository_facts import canonical_sha256 as _canonical_sha256

RUNTIME_BASELINE_SCHEMA = "tulid.runtime-baseline/v1"
RUNTIME_BASELINE_FILENAME = "runtime-baseline.json"


@dataclass(frozen=True)
class RuntimeBaseline:
    schema: str
    source_root: str | None
    source_revision: str | None
    source_dirty: bool | None
    dirty_patch_identity: str | None
    installed_tulid_location: str | None
    installed_tulid_revision: str | None
    workflow_hash: str
    command_policy_hash: str | None
    worker_image_identity: str | None
    worker_id: str
    effective_worker_config: Mapping[str, object]
    sha256: str


def capture_runtime_baseline(
    *,
    source_root: Path | None,
    workflow: WorkflowDefinition,
    worker_id: str,
    worker_images: Mapping[str, str] | None = None,
    worker_types: Mapping[str, str] | None = None,
    worker_args: Mapping[str, tuple[str, ...]] | None = None,
    default_timeout_seconds: int = 7200,
    max_repair_attempts: int = 2,
    command_policy_sha256: str | None = None,
    tulid_location: Path | None = None,
) -> RuntimeBaseline:
    """Capture a reproducible baseline for one worker fixture run.

    The source revision and working-tree patch identity come from
    ``source_root`` (the checkout being edited). The installed Tulid location
    and its own revision are captured from ``tulid_location`` and are never
    assumed to be ``source_root``. The worker image identity and a sanitized
    worker configuration are recorded for reproducibility.
    """
    source_revision, source_dirty, dirty_patch_identity = _source_facts(source_root)
    installed_tulid_revision = _git_revision(tulid_location)
    effective = _effective_worker_config(
        worker_id=worker_id,
        worker_images=worker_images or {},
        worker_types=worker_types or {},
        worker_args=worker_args or {},
        default_timeout_seconds=default_timeout_seconds,
        max_repair_attempts=max_repair_attempts,
    )
    payload = {
        "schema": RUNTIME_BASELINE_SCHEMA,
        "source_root": str(source_root) if source_root is not None else None,
        "source_revision": source_revision,
        "source_dirty": source_dirty,
        "dirty_patch_identity": dirty_patch_identity,
        "installed_tulid_location": str(tulid_location) if tulid_location is not None else None,
        "installed_tulid_revision": installed_tulid_revision,
        "workflow_hash": workflow_sha256(workflow),
        "command_policy_hash": command_policy_sha256,
        "worker_image_identity": effective.get("worker_image"),
        "worker_id": worker_id,
        "effective_worker_config": effective,
    }
    return RuntimeBaseline(
        schema=RUNTIME_BASELINE_SCHEMA,
        source_root=str(source_root) if source_root is not None else None,
        source_revision=source_revision,
        source_dirty=source_dirty,
        dirty_patch_identity=dirty_patch_identity,
        installed_tulid_location=str(tulid_location) if tulid_location is not None else None,
        installed_tulid_revision=installed_tulid_revision,
        workflow_hash=(payload.get("workflow_hash") or ""),
        command_policy_hash=command_policy_sha256,
        worker_image_identity=effective.get("worker_image"),
        worker_id=worker_id,
        effective_worker_config=effective,
        sha256=_canonical_sha256(payload),
    )


def workload_definition_sha256(workflow: WorkflowDefinition) -> str:
    """Deterministic hash over the compiled workflow model (compatibility alias)."""
    return workflow_sha256(workflow)


def workflow_sha256(workflow: WorkflowDefinition) -> str:
    """Deterministic hash over a compiled :class:`WorkflowDefinition`."""
    payload = _workflow_payload(workflow)
    return _canonical_sha256(payload)


def _workflow_payload(workflow: WorkflowDefinition) -> dict[str, Any]:
    return {
        "schema_version": workflow.schema_version,
        "states": sorted(workflow.states),
        "task_types": sorted(workflow.task_types),
        "worker_ids": sorted(
            worker.id for worker in workflow.workers.values()
        ),
        "worker_bindings": sorted(
            (worker.id, worker.type, worker.implementation_id)
            for worker in workflow.workers.values()
        ),
        "transitions": sorted(
            (
                transition.id,
                transition.task_type,
                transition.from_state,
                transition.to_state,
                transition.worker,
                sorted(transition.requires.artifacts),
                sorted((call.type, dict(call.args)) for call in transition.requires.validations),
                transition.requires.changed_files_required,
            )
            for transition in workflow.transitions.values()
        ),
    }


def baseline_to_dict(baseline: RuntimeBaseline) -> dict[str, Any]:
    return {
        "schema": baseline.schema,
        "source_root": baseline.source_root,
        "source_revision": baseline.source_revision,
        "source_dirty": baseline.source_dirty,
        "dirty_patch_identity": baseline.dirty_patch_identity,
        "installed_tulid_location": baseline.installed_tulid_location,
        "installed_tulid_revision": baseline.installed_tulid_revision,
        "workflow_hash": baseline.workflow_hash,
        "command_policy_hash": baseline.command_policy_hash,
        "worker_image_identity": baseline.worker_image_identity,
        "worker_id": baseline.worker_id,
        "effective_worker_config": dict(baseline.effective_worker_config),
        "sha256": baseline.sha256,
    }


def baseline_from_dict(payload: Mapping[str, Any]) -> RuntimeBaseline:
    return RuntimeBaseline(
        schema=str(payload.get("schema", RUNTIME_BASELINE_SCHEMA)),
        source_root=_optional_str(payload.get("source_root")),
        source_revision=_optional_str(payload.get("source_revision")),
        source_dirty=payload.get("source_dirty"),
        dirty_patch_identity=_optional_str(payload.get("dirty_patch_identity")),
        installed_tulid_location=_optional_str(payload.get("installed_tulid_location")),
        installed_tulid_revision=_optional_str(payload.get("installed_tulid_revision")),
        workflow_hash=str(payload.get("workflow_hash", "")),
        command_policy_hash=_optional_str(payload.get("command_policy_hash")),
        worker_image_identity=_optional_str(payload.get("worker_image_identity")),
        worker_id=str(payload.get("worker_id", "")),
        effective_worker_config=dict(payload.get("effective_worker_config") or {}),
        sha256=str(payload.get("sha256", "")),
    )


def write_runtime_baseline(baseline: RuntimeBaseline, workspace: Path) -> Path:
    """Persist a baseline into an isolated fixture run's workspace."""
    target = workspace / ".open-tulid" / RUNTIME_BASELINE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(baseline_to_dict(baseline), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _source_facts(root: Path | None) -> tuple[str | None, bool | None, str | None]:
    if root is None or not root.is_dir():
        return None, None, None
    revision = _git_revision(root)
    if revision is None:
        return None, None, None
    patch = _git_diff_patch(root)
    dirty = patch is not None and bool(patch.strip())
    identity = _canonical_sha256({"patch": patch or ""}) if patch is not None else None
    return revision, dirty, identity


def _git_diff_patch(root: Path) -> str | None:
    return _run_git(root, "diff", "HEAD", "--no-ext-diff", "--no-color")


def _git_revision(root: Path | None) -> str | None:
    if root is None:
        return None
    return _run_git(root, "rev-parse", "--verify", "HEAD")


def _run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _effective_worker_config(
    *,
    worker_id: str,
    worker_images: Mapping[str, str],
    worker_types: Mapping[str, str],
    worker_args: Mapping[str, tuple[str, ...]],
    default_timeout_seconds: int,
    max_repair_attempts: int,
) -> dict[str, object]:
    return {
        "worker_id": worker_id,
        "worker_image": worker_images.get(worker_id),
        "worker_type": worker_types.get(worker_id),
        "worker_args": _sanitize_args(worker_args.get(worker_id, ())),
        "default_timeout_seconds": default_timeout_seconds,
        "max_repair_attempts": max_repair_attempts,
    }


def _sanitize_args(args: tuple[str, ...]) -> tuple[str, ...]:
    def redact(arg: str) -> str:
        key, separator, _value = arg.partition("=")
        if separator and (
            "TOKEN" in key.upper()
            or "SECRET" in key.upper()
            or key.upper().endswith("_KEY")
        ):
            return f"{key}=<redacted>"
        return arg

    return tuple(redact(arg) for arg in args)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
