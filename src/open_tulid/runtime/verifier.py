from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from open_tulid.domain import DomainError, TransitionDefinition


@dataclass(frozen=True)
class CompletionSubmission:
    summary: str = ""
    artifacts: tuple[str, ...] = ()
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
    def verify(
        self,
        *,
        workspace: Path,
        transition: TransitionDefinition,
        submission: CompletionSubmission,
    ) -> VerificationResult:
        errors: list[DomainError] = []
        artifact_set = set(submission.artifacts)

        for artifact in transition.requires.artifacts:
            if artifact not in artifact_set:
                errors.append(_error(
                    "completion.artifact_missing",
                    f"Required artifact was not submitted: {artifact}",
                    artifact,
                ))

        for artifact in submission.artifacts:
            artifact_path = _workspace_path(workspace, artifact)
            if artifact_path is None:
                errors.append(_error(
                    "completion.artifact_outside_workspace",
                    f"Artifact path escapes the workspace: {artifact}",
                    artifact,
                ))
                continue
            if not artifact_path.is_file():
                errors.append(_error(
                    "completion.artifact_not_found",
                    f"Submitted artifact does not exist: {artifact}",
                    artifact,
                ))
                continue
            if artifact_path.stat().st_size == 0:
                errors.append(_error(
                    "completion.artifact_empty",
                    f"Submitted artifact is empty: {artifact}",
                    artifact,
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

        for changed_file in submission.changed_files:
            if _workspace_path(workspace, changed_file) is None:
                errors.append(_error(
                    "completion.changed_file_outside_workspace",
                    f"Changed file path escapes the workspace: {changed_file}",
                    changed_file,
                ))

        return VerificationResult(accepted=not errors, errors=tuple(errors))


def submission_from_mapping(payload: Mapping[str, object]) -> CompletionSubmission:
    evidence = payload.get("validation_evidence", {})
    if not isinstance(evidence, Mapping):
        evidence = {}
    return CompletionSubmission(
        summary=str(payload.get("summary", "")),
        artifacts=_string_tuple(payload.get("artifacts", ())),
        changed_files=_string_tuple(payload.get("changed_files", ())),
        validation_evidence={str(key): str(value) for key, value in evidence.items()},
    )


def _workspace_path(workspace: Path, value: str) -> Path | None:
    candidate = (workspace / value).resolve()
    root = workspace.resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
