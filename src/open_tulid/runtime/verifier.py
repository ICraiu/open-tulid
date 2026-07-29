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
from open_tulid.runtime.repository_facts import BaselineManifest, capture_repository_snapshot


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
    report: "VerificationReport | None" = None

    @property
    def message(self) -> str:
        if self.accepted:
            return "Completion accepted."
        return "; ".join(error.message for error in self.errors)


@dataclass(frozen=True)
class VerificationCheckResult:
    id: str
    status: str
    argv: tuple[str, ...]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "status": self.status, "argv": list(self.argv),
            "exit_code": self.exit_code, "stdout": self.stdout, "stderr": self.stderr,
        }


@dataclass(frozen=True)
class VerificationReport:
    schema: str
    classification: str | None
    baseline_sha256: str
    post_manifest_sha256: str | None
    added: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    changed_lines: int = 0
    checks: tuple[VerificationCheckResult, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema, "classification": self.classification,
            "baseline_sha256": self.baseline_sha256,
            "post_manifest_sha256": self.post_manifest_sha256,
            "changes": {"added": list(self.added), "edited": list(self.edited),
                        "removed": list(self.removed),
                        "renamed": [{"from": old, "to": new} for old, new in self.renamed],
                        "changed_lines": self.changed_lines},
            "checks": [check.to_dict() for check in self.checks],
        }


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
        report: VerificationReport | None = None
        if (
            execution_contract is not None
            and execution_contract.transition != transition
        ):
            errors.append(_error(
                "execution_contract.transition_mismatch",
                "Verifier transition does not match the frozen execution contract.",
                transition.id,
            ))
        if execution_contract is not None:
            report, enforcement_errors = self._enforce_execution_contract(
                workspace=workspace,
                contract=execution_contract,
            )
            errors.extend(enforcement_errors)
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

        if report is not None and errors:
            report = VerificationReport(
                schema=report.schema,
                classification=_failure_classification(errors),
                baseline_sha256=report.baseline_sha256,
                post_manifest_sha256=report.post_manifest_sha256,
                added=report.added,
                edited=report.edited,
                removed=report.removed,
                renamed=report.renamed,
                changed_lines=report.changed_lines,
                checks=report.checks,
            )
        return VerificationResult(accepted=not errors, errors=tuple(errors), report=report)

    def _enforce_execution_contract(
        self,
        *,
        workspace: Path,
        contract: ExecutionContract,
    ) -> tuple[VerificationReport, tuple[DomainError, ...]]:
        errors: list[DomainError] = []
        post = capture_repository_snapshot(workspace)
        if not post.accepted or post.snapshot is None:
            errors.extend(_error("verification.baseline_unavailable", error.message, error.location) for error in post.errors)
            return VerificationReport("tulid.verification/v1", "baseline_failure", contract.baseline_manifest.sha256, None), tuple(errors)
        added, edited, removed, renamed = _manifest_changes(contract.baseline_manifest, post.snapshot.baseline)
        surface = contract.generated_contract.change_surface
        for path in added:
            if not _path_allowed(path, surface.add):
                errors.append(_error("verification.path_add_forbidden", f"Added file is outside the allowed add surface: {path}", path))
        for path in edited:
            if not _path_allowed(path, surface.edit):
                errors.append(_error("verification.path_edit_forbidden", f"Edited file is outside the allowed edit surface: {path}", path))
        for path in removed:
            errors.append(_error("verification.deletion_forbidden", f"Contract does not permit deleting files: {path}", path))
        for old, new in renamed:
            errors.append(_error("verification.rename_forbidden", f"Contract does not permit renaming files: {old} -> {new}", new))
        for path in (*added, *edited, *removed):
            if _path_allowed(path, surface.forbidden):
                errors.append(_error("verification.path_forbidden", f"Contract forbids changing: {path}", path))
        changed_lines = _changed_line_count(workspace, contract.baseline_manifest, (*added, *edited, *removed))
        changed_file_count = len(added) + len(edited) + len(removed) + (2 * len(renamed))
        if surface.max_files is not None and changed_file_count > surface.max_files:
            errors.append(_error("verification.max_files_exceeded", f"Contract permits at most {surface.max_files} changed files; found {changed_file_count}."))
        if surface.max_changed_lines is not None and changed_lines > surface.max_changed_lines:
            errors.append(_error("verification.changed_line_budget_exceeded", f"Contract permits at most {surface.max_changed_lines} changed lines; conservative count is {changed_lines}."))
        checks, check_errors = _run_contract_checks(workspace, contract)
        errors.extend(check_errors)
        return VerificationReport(
            "tulid.verification/v1", None, contract.baseline_manifest.sha256,
            post.snapshot.baseline.sha256, added, edited, removed, renamed,
            changed_lines, checks,
        ), tuple(errors)

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


def _manifest_changes(
    baseline: BaselineManifest,
    post: BaselineManifest,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
    before = {entry.path: entry for entry in baseline.entries}
    after = {entry.path: entry for entry in post.entries}
    added = set(after) - set(before)
    removed = set(before) - set(after)
    edited = tuple(sorted(path for path in set(before) & set(after) if before[path].sha256 != after[path].sha256))
    renamed: list[tuple[str, str]] = []
    for old in sorted(removed):
        matches = sorted(new for new in added if before[old].sha256 == after[new].sha256)
        if len(matches) == 1:
            new = matches[0]
            renamed.append((old, new))
            added.remove(new)
            removed.remove(old)
    return tuple(sorted(added)), edited, tuple(sorted(removed)), tuple(renamed)


def _path_allowed(path: str, patterns: Sequence[str]) -> bool:
    from fnmatch import fnmatchcase
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _changed_line_count(workspace: Path, baseline: BaselineManifest, paths: Sequence[str]) -> int:
    total = 0
    for path in paths:
        new_path = workspace / path
        new = _read_text(new_path) if new_path.is_file() else ""
        if new is None:
            total += 1
            continue
        # The frozen manifest deliberately contains only hashes and sizes, never
        # source content. Count the complete current file as a conservative upper
        # bound for each added or edited file; removals count as one unit.
        total += max(1, len(new.splitlines()))
    return total


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _run_contract_checks(
    workspace: Path,
    contract: ExecutionContract,
) -> tuple[tuple[VerificationCheckResult, ...], tuple[DomainError, ...]]:
    results: list[VerificationCheckResult] = []
    errors: list[DomainError] = []
    for check in contract.resolved_checks:
        if check.runner != "command":
            continue
        cwd = _contained_path(workspace, check.working_directory)
        if cwd is None or not cwd.is_dir():
            results.append(VerificationCheckResult(check.id, "environment_error", check.argv, stderr="working directory unavailable"))
            errors.append(_error("verification.check_environment", f"Check {check.id!r} has no usable working directory.", check.id))
            continue
        try:
            completed = subprocess.run(check.argv, cwd=cwd, capture_output=True, text=True, timeout=check.timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            results.append(VerificationCheckResult(check.id, "timeout", check.argv, stdout=_as_text(exc.stdout), stderr=_as_text(exc.stderr)))
            errors.append(_error("verification.check_timeout", f"Frozen check timed out: {check.id}", check.id))
            continue
        except OSError as exc:
            results.append(VerificationCheckResult(check.id, "environment_error", check.argv, stderr=str(exc)))
            errors.append(_error("verification.check_environment", f"Frozen check could not run: {check.id}: {exc}", check.id))
            continue
        passed = (completed.returncode == check.expect.exit_code
                  and all(value in completed.stdout for value in check.expect.stdout_contains)
                  and all(value in completed.stderr for value in check.expect.stderr_contains))
        results.append(VerificationCheckResult(check.id, "passed" if passed else "failed", check.argv, completed.returncode, completed.stdout, completed.stderr))
        if not passed:
            errors.append(_error("verification.check_failed", f"Frozen check failed expectations: {check.id}", check.id))
    return tuple(results), tuple(errors)


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _failure_classification(errors: Sequence[DomainError]) -> str:
    codes = {error.code for error in errors}
    if any(code.startswith("verification.baseline") for code in codes):
        return "baseline_failure"
    if any(code in {"verification.check_environment", "verification.check_timeout"} for code in codes):
        return "environment_failure"
    if any(code.startswith("execution_contract") or code.startswith("verification.path") or code.startswith("verification.deletion") or code.startswith("verification.rename") or code.startswith("verification.max_files") or code.startswith("verification.changed_line_budget") for code in codes):
        return "contract_failure"
    return "implementation_failure"


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
