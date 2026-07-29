from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from open_tulid.domain import DomainError, TransitionDefinition
from open_tulid.runtime.execution_contracts import ExecutionContract


@dataclass(frozen=True)
class ArtifactSubmission:
    type: str
    path: str
    sha256: str | None = None


@dataclass(frozen=True)
class CompletionSubmission:
    submission_id: str | None = None
    attempt: int | None = None
    summary: str = ""
    artifacts: tuple[ArtifactSubmission, ...] = ()
    changed_files: tuple[str, ...] = ()
    validation_evidence: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationResult:
    accepted: bool
    errors: tuple[DomainError, ...] = ()

    @property
    def message(self) -> str:
        if self.accepted:
            return "Completion accepted."
        return "; ".join(error.message for error in self.errors)


class DeterministicVerifier:
    def __init__(
        self,
        *,
        artifact_templates: Mapping[str, str | None] | None = None,
        validation_implementations: Mapping[str, Callable[..., object]] | None = None,
        validation_context_factory: Callable[[Path, Path], object] | None = None,
    ) -> None:
        self.artifact_templates = dict(artifact_templates or {})
        self.validation_implementations = dict(validation_implementations or {})
        self.validation_context_factory = validation_context_factory

    def verify(
        self,
        *,
        workspace: Path,
        output_dir: Path | None = None,
        transition: TransitionDefinition,
        submission: CompletionSubmission,
        execution_contract: ExecutionContract | None = None,
    ) -> VerificationResult:
        errors: list[DomainError] = []
        if (
            execution_contract is not None
            and execution_contract.transition != transition
        ):
            errors.append(_error(
                "execution_contract.transition_mismatch",
                "Verifier transition does not match the frozen execution contract.",
                transition.id,
            ))
        output_root = output_dir or workspace / "output"
        submitted_artifacts = normalize_artifacts(submission.artifacts)
        duplicate_artifact_types = _duplicates(artifact.type for artifact in submitted_artifacts)
        duplicate_artifact_paths = _duplicates(artifact.path for artifact in submitted_artifacts)
        duplicate_changed_files = _duplicates(submission.changed_files)
        multi_artifact_type = transition.derives.artifact_type if transition.derives is not None else None
        for artifact_type in duplicate_artifact_types:
            if artifact_type == multi_artifact_type:
                continue
            errors.append(_error(
                "completion.artifact_duplicate_type",
                f"Artifact type was submitted more than once: {artifact_type}",
                artifact_type,
            ))
        for artifact_path in duplicate_artifact_paths:
            errors.append(_error(
                "completion.artifact_duplicate_path",
                f"Artifact path was submitted more than once: {artifact_path}",
                artifact_path,
            ))
        for changed_file in duplicate_changed_files:
            errors.append(_error(
                "completion.changed_file_duplicate",
                f"Changed file was submitted more than once: {changed_file}",
                changed_file,
            ))
        artifact_types = {artifact.type for artifact in submitted_artifacts}

        for artifact in transition.requires.artifacts:
            if artifact not in artifact_types:
                errors.append(_error(
                    "completion.artifact_missing",
                    f"Required artifact was not submitted: {artifact}",
                    artifact,
                ))

        allowed_artifact_types = set(transition.requires.artifacts)
        if multi_artifact_type is not None:
            allowed_artifact_types.add(multi_artifact_type)
            if multi_artifact_type not in artifact_types:
                errors.append(_error(
                    "completion.derived_task_missing",
                    f"Deriving transition requires at least one {multi_artifact_type} artifact.",
                    multi_artifact_type,
                ))

        for artifact in submitted_artifacts:
            if artifact.type not in allowed_artifact_types:
                errors.append(_error(
                    "completion.artifact_unexpected",
                    f"Artifact type is not required by this transition: {artifact.type}",
                    artifact.type,
                ))
            template = self.artifact_templates.get(artifact.type)
            if template and "{" not in template and artifact.path != template:
                errors.append(_error(
                    "completion.artifact_template_mismatch",
                    f"Artifact path must match template for {artifact.type}: {template}",
                    artifact.path,
                ))
            artifact_path = _contained_path(output_root, artifact.path)
            if artifact_path is None:
                errors.append(_error(
                    "completion.artifact_outside_output",
                    f"Artifact path escapes the shared output directory: {artifact.path}",
                    artifact.path,
                ))
                continue
            if _escapes_via_symlink(output_root, artifact.path):
                errors.append(_error(
                    "completion.artifact_symlink_escape",
                    f"Artifact path escapes the shared output directory through a symlink: {artifact.path}",
                    artifact.path,
                ))
                continue
            if not artifact_path.is_file():
                errors.append(_error(
                    "completion.artifact_not_found",
                    f"Submitted artifact does not exist: {artifact.path}",
                    artifact.path,
                ))
                continue
            if artifact_path.stat().st_size == 0:
                errors.append(_error(
                    "completion.artifact_empty",
                    f"Submitted artifact is empty: {artifact.path}",
                    artifact.path,
                ))
            if artifact.sha256 is not None:
                actual_hash = _sha256(artifact_path)
                if actual_hash != artifact.sha256.lower():
                    errors.append(_error(
                        "completion.artifact_hash_mismatch",
                        f"Submitted artifact hash does not match: {artifact.path}",
                        artifact.path,
                    ))

        required_validations = tuple(call.type for call in transition.requires.validations)
        for validation in required_validations:
            evidence = submission.validation_evidence.get(validation)
            if evidence is None or not str(evidence).strip():
                errors.append(_error(
                    "completion.validation_evidence_missing",
                    f"Validation evidence is missing for {validation}.",
                    validation,
                ))
        errors.extend(self._run_trusted_validations(
            workspace=workspace,
            output_root=output_root,
            transition=transition,
        ))

        if transition.requires.changed_files_required and not submission.changed_files:
            errors.append(_error(
                "completion.changed_files_missing",
                "Changed-file evidence is required for this transition.",
            ))

        for changed_file in submission.changed_files:
            changed_path = _contained_path(workspace, changed_file)
            if changed_path is None:
                errors.append(_error(
                    "completion.changed_file_outside_workspace",
                    f"Changed file path escapes the workspace: {changed_file}",
                    changed_file,
                ))
            elif not changed_path.exists():
                errors.append(_error(
                    "completion.changed_file_not_found",
                    f"Changed file does not exist: {changed_file}",
                    changed_file,
                ))

        actual_changed_files = _git_changed_files(workspace)
        if actual_changed_files is not None:
            submitted = set(submission.changed_files)
            if submitted != actual_changed_files:
                errors.append(_error(
                    "completion.changed_files_mismatch",
                    "Submitted changed files do not match the workspace diff.",
                    ",".join(sorted(actual_changed_files)),
                ))

        return VerificationResult(accepted=not errors, errors=tuple(errors))

    def _run_trusted_validations(
        self,
        *,
        workspace: Path,
        output_root: Path,
        transition: TransitionDefinition,
    ) -> tuple[DomainError, ...]:
        errors: list[DomainError] = []
        if not transition.requires.validations:
            return ()
        if self.validation_context_factory is None:
            return tuple(_error(
                "completion.validation_unavailable",
                "Trusted validation runtime is not configured.",
                call.type,
            ) for call in transition.requires.validations)
        with tempfile.TemporaryDirectory(prefix="open-tulid-validation-") as temp_dir:
            validation_workspace = Path(temp_dir) / "workspace"
            shutil.copytree(workspace, validation_workspace, symlinks=True)
            _make_tree_user_writable(validation_workspace)
            validation_output = _map_validation_output_root(
                workspace=workspace,
                output_root=output_root,
                validation_workspace=validation_workspace,
            )
            context = self.validation_context_factory(validation_workspace, validation_output)
            for call in transition.requires.validations:
                implementation = self.validation_implementations.get(call.type)
                if implementation is None:
                    errors.append(_error(
                        "completion.validation_unimplemented",
                        f"No trusted validation implementation is installed for {call.type}.",
                        call.type,
                    ))
                    continue
                try:
                    result = implementation(context, **dict(call.args))
                except Exception as exc:
                    errors.append(_error(
                        "completion.validation_error",
                        f"Trusted validation {call.type} raised: {exc}",
                        call.type,
                    ))
                    continue
                if not bool(getattr(result, "passed", False)):
                    message = str(getattr(result, "message", "") or "validation failed")
                    errors.append(_error(
                        "completion.validation_failed",
                        f"Trusted validation {call.type} failed: {message}",
                        call.type,
                    ))
        return tuple(errors)


def submission_from_mapping(payload: Mapping[str, object]) -> CompletionSubmission:
    evidence = payload.get("validation_evidence", {})
    if not isinstance(evidence, Mapping):
        evidence = {}
    return CompletionSubmission(
        submission_id=_optional_string(payload.get("submission_id")),
        attempt=_optional_int(payload.get("attempt")),
        summary=str(payload.get("summary", "")),
        artifacts=_artifact_tuple(payload.get("artifacts", ())),
        changed_files=_string_tuple(payload.get("changed_files", ())),
        validation_evidence={str(key): str(value) for key, value in evidence.items()},
    )


def _contained_path(root_path: Path, value: str) -> Path | None:
    if not value or Path(value).is_absolute():
        return None
    candidate = (root_path / value).resolve()
    root = root_path.resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def _escapes_via_symlink(root_path: Path, value: str) -> bool:
    root = root_path.resolve()
    current = root
    for part in Path(value).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return True
        current = current / part
        if current.is_symlink():
            resolved = current.resolve()
            if resolved != root and root not in resolved.parents:
                return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_tuple(value: object) -> tuple[ArtifactSubmission, ...]:
    if isinstance(value, str):
        return (ArtifactSubmission(type=value, path=value),)
    if not isinstance(value, Sequence):
        return ()
    artifacts: list[ArtifactSubmission] = []
    for item in value:
        if isinstance(item, str):
            clean = item.strip()
            if clean:
                artifacts.append(ArtifactSubmission(type=clean, path=clean))
            continue
        if not isinstance(item, Mapping):
            continue
        artifact_type = _optional_string(item.get("type"))
        path = _optional_string(item.get("path"))
        if artifact_type is None or path is None:
            continue
        artifacts.append(ArtifactSubmission(
            type=artifact_type,
            path=path,
            sha256=_optional_string(item.get("sha256")),
        ))
    return tuple(artifacts)


def normalize_artifacts(value: Sequence[ArtifactSubmission | str]) -> tuple[ArtifactSubmission, ...]:
    artifacts: list[ArtifactSubmission] = []
    for item in value:
        if isinstance(item, ArtifactSubmission):
            artifacts.append(item)
        else:
            clean = str(item).strip()
            if clean:
                artifacts.append(ArtifactSubmission(type=clean, path=clean))
    return tuple(artifacts)


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _duplicates(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for raw in values:
        value = str(raw)
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)


def _map_validation_output_root(*, workspace: Path, output_root: Path, validation_workspace: Path) -> Path:
    try:
        relative_output = output_root.resolve().relative_to(workspace.resolve())
    except ValueError:
        return validation_workspace / "output"
    return validation_workspace / relative_output


def _make_tree_user_writable(root: Path) -> None:
    for path in tuple(root.rglob("*")) + (root,):
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            continue
        writable_mode = mode | stat.S_IRUSR | stat.S_IWUSR
        if stat.S_ISDIR(mode):
            writable_mode |= stat.S_IXUSR
        try:
            path.chmod(writable_mode)
        except OSError:
            continue


def _git_changed_files(workspace: Path) -> set[str] | None:
    if not (workspace / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    changed: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            changed.add(path)
    return changed
