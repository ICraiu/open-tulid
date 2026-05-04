from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from open_tulid.domain.schema import (
    Artifact,
    ArtifactRegistry,
    ArtifactState,
    ValidationReport,
)
from open_tulid.domain.readers import read_artifact_file
from open_tulid.domain.templates import (
    build_completed_task_template,
    build_defined_task_template,
    build_idea_task_template,
    build_technical_direction_template,
    build_technical_spec_template,
)
from open_tulid.domain.validation import validate_artifact
from open_tulid.models import Project, ValidationError, ValidationReport as VaultValidationReport


TASK_STATES = [
    (ArtifactState.CompletedTask, build_completed_task_template),
    (ArtifactState.DefinedTask, build_defined_task_template),
    (ArtifactState.IdeaTask, build_idea_task_template),
]

DOC_STATES = [
    (ArtifactState.TechnicalDirection, build_technical_direction_template),
    (ArtifactState.TechnicalSpec, build_technical_spec_template),
]

READER_ERROR_PREFIXES = {"path", "preamble"}


def _is_reader_error(error_location: str) -> bool:
    for prefix in READER_ERROR_PREFIXES:
        if error_location == prefix or error_location.startswith(prefix + "."):
            return True
        if error_location.startswith("section.") and "Duplicate section" in error_location or "Duplicate field" in error_location:
            return True
    return False


@dataclass
class ArtifactReadAttempt:
    artifact: Artifact | None = None
    state: ArtifactState | None = None
    report: ValidationReport = field(default_factory=ValidationReport)


def iter_task_artifact_files(project: Project) -> list[Path]:
    tasks_dir = project.path / "tasks"
    if not tasks_dir.is_dir():
        return []
    return sorted(
        f for f in tasks_dir.iterdir()
        if f.is_file() and f.name.endswith(".md")
    )


def iter_doc_artifact_files(project: Project) -> list[Path]:
    docs_dir = project.path / "docs"
    if not docs_dir.is_dir():
        return []
    return sorted(
        f for f in docs_dir.iterdir()
        if f.is_file() and f.name.endswith(".md")
    )


def _path_to_domain_string(path: Path, project: Project) -> str:
    try:
        return Path(os.path.relpath(path, project.path)).as_posix()
    except ValueError:
        return path.as_posix()


def _build_registry_aliases(artifact: Artifact, project: Project) -> list[str]:
    aliases: list[str] = []

    canonical = artifact.path
    if canonical:
        canonical_posix = Path(canonical).as_posix()
        aliases.append(canonical_posix)

    if canonical:
        canonical_posix = Path(canonical).as_posix()
        file_path = Path(project.path) / canonical_posix
        abs_path = str(file_path.resolve())
        if abs_path not in aliases:
            aliases.append(abs_path)

        vault_rel = str(project.name) + "/" + canonical_posix
        if vault_rel not in aliases:
            aliases.append(vault_rel)

    return aliases


def read_task_artifact_candidate(
    path: Path,
    project: Project,
    registry: ArtifactRegistry | None = None,
) -> ArtifactReadAttempt:
    attempt = ArtifactReadAttempt()
    domain_path = _path_to_domain_string(path, project)

    matched = 0
    reader_errors: list = []
    for state, template_builder in TASK_STATES:
        template = template_builder()
        result = read_artifact_file(
            str(path), state, template, registry, validate_links=False, domain_path=domain_path
        )

        if result.is_valid:
            attempt.artifact = result.artifact
            attempt.state = state
            attempt.report = result.report
            return attempt

        for err in result.report.errors:
            if _is_reader_error(err.location):
                reader_errors.append(err)

        matched += 1

    if reader_errors:
        attempt.report = ValidationReport()
        for err in reader_errors:
            attempt.report.errors.append(err)
    else:
        attempt.report = ValidationReport()
        attempt.report.add_error(
            "artifact",
            f"File '{domain_path}' does not match any supported task artifact state",
            path=domain_path,
        )
    return attempt


def read_doc_artifact_candidate(
    path: Path,
    project: Project,
    registry: ArtifactRegistry | None = None,
) -> ArtifactReadAttempt:
    attempt = ArtifactReadAttempt()
    domain_path = _path_to_domain_string(path, project)

    matched = 0
    reader_errors: list = []
    for state, template_builder in DOC_STATES:
        template = template_builder()
        result = read_artifact_file(
            str(path), state, template, registry, validate_links=False, domain_path=domain_path
        )

        if result.is_valid:
            attempt.artifact = result.artifact
            attempt.state = state
            attempt.report = result.report
            return attempt

        for err in result.report.errors:
            if _is_reader_error(err.location):
                reader_errors.append(err)

        matched += 1

    if reader_errors:
        attempt.report = ValidationReport()
        for err in reader_errors:
            attempt.report.errors.append(err)

    return attempt


def _convert_domain_errors_to_vault(
    domain_report: ValidationReport, default_path: str | None = None
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for err in domain_report.errors:
        path_val = Path(err.path) if err.path else (Path(default_path) if default_path else None)
        message = f"Domain artifact validation failed at {err.location}: {err.message}"
        errors.append(ValidationError(
            path=path_val,
            line=None,
            message=message,
        ))
    return errors


def _build_doc_candidate_alias_lookup(doc_files: list[Path], project: Project) -> dict[str, Path]:
    lookup: dict[str, Path] = {}
    for doc_file in doc_files:
        doc_domain_path = _path_to_domain_string(doc_file, project)
        lookup[doc_domain_path] = doc_file
        abs_path = str(doc_file.resolve())
        lookup[abs_path] = doc_file
        vault_rel = str(project.name) + "/" + doc_domain_path
        lookup[vault_rel] = doc_file
    return lookup


def validate_project_domain_artifacts(project: Project) -> VaultValidationReport:
    report = VaultValidationReport()

    task_files = iter_task_artifact_files(project)
    doc_files = iter_doc_artifact_files(project)
    doc_file_set = set(str(f) for f in doc_files)
    doc_candidate_lookup = _build_doc_candidate_alias_lookup(doc_files, project)

    # Pass 1: Read all candidate artifacts without link validation
    all_artifacts: list[tuple[Path, ArtifactReadAttempt]] = []
    for task_file in task_files:
        attempt = read_task_artifact_candidate(task_file, project)
        all_artifacts.append((task_file, attempt))

    for doc_file in doc_files:
        attempt = read_doc_artifact_candidate(doc_file, project)
        all_artifacts.append((doc_file, attempt))

    # Build registry from successfully parsed artifacts
    # Also track doc aliases that failed to parse (exist as files but weren't registered)
    registry = ArtifactRegistry()
    failed_doc_aliases: set[str] = set()
    for file_path, attempt in all_artifacts:
        if attempt.artifact is not None:
            aliases = _build_registry_aliases(attempt.artifact, project)
            for alias in aliases:
                if registry.contains(alias):
                    existing = registry.get(alias)
                    if existing is not attempt.artifact:
                        domain_report = ValidationReport()
                        domain_report.add_error(
                            "registry",
                            f"Duplicate alias '{alias}' points to different artifacts",
                            path=alias,
                        )
                        report.errors.extend(_convert_domain_errors_to_vault(
                            domain_report, str(file_path)
                        ))
                else:
                    registry.artifacts_by_path[alias] = attempt.artifact
        else:
            # Track doc file aliases that failed to parse
            if str(file_path) in doc_file_set:
                domain_path = _path_to_domain_string(file_path, project)
                failed_doc_aliases.add(domain_path)
                failed_doc_aliases.add(str(file_path.resolve()))
                failed_doc_aliases.add(str(project.name) + "/" + domain_path)

    # Find referenced doc paths from successfully parsed task artifacts
    referenced_doc_paths: set[str] = set()
    for file_path, attempt in all_artifacts:
        if attempt.artifact is None:
            continue
        if attempt.state not in (ArtifactState.DefinedTask, ArtifactState.CompletedTask):
            continue
        for section in attempt.artifact.sections:
            for field_item in section.fields:
                if field_item.type.name in ("FILE", "FILE_LIST"):
                    if isinstance(field_item.value, str) and field_item.value:
                        referenced_doc_paths.add(field_item.value)
                    elif isinstance(field_item.value, list):
                        referenced_doc_paths.update(field_item.value)

    # Validate referenced docs that were not parsed in pass 1
    for doc_path_str in referenced_doc_paths:
        # Check if it's a registry alias first
        existing = registry.get(doc_path_str)
        if existing is not None:
            continue

        # Resolve to direct docs candidate via the alias lookup
        doc_file_path = doc_candidate_lookup.get(doc_path_str)
        if doc_file_path is None:
            # Try resolving as project-relative path
            doc_path = project.path / doc_path_str
            if str(doc_path) in doc_file_set:
                doc_file_path = doc_path
            else:
                # Not a direct candidate - skip silently
                continue

        domain_path = _path_to_domain_string(doc_file_path, project)
        existing = registry.get(domain_path)
        if existing is not None:
            continue

        attempt = read_doc_artifact_candidate(doc_file_path, project, registry)
        if attempt.artifact is not None:
            aliases = _build_registry_aliases(attempt.artifact, project)
            for alias in aliases:
                if registry.contains(alias):
                    if registry.get(alias) is not attempt.artifact:
                        domain_report = ValidationReport()
                        domain_report.add_error(
                            "registry",
                            f"Duplicate alias '{alias}' points to different artifacts",
                            path=alias,
                        )
                        report.errors.extend(_convert_domain_errors_to_vault(
                            domain_report, str(doc_file_path)
                        ))
                else:
                    registry.artifacts_by_path[alias] = attempt.artifact
            full_report = validate_artifact(attempt.artifact, registry)
            report.errors.extend(_convert_domain_errors_to_vault(
                full_report, domain_path
            ))
        else:
            if not attempt.report.errors:
                attempt.report.add_error(
                    "artifact",
                    f"Referenced doc file '{domain_path}' does not match any supported doc artifact state",
                    path=domain_path,
                )
            report.errors.extend(_convert_domain_errors_to_vault(
                attempt.report, domain_path
            ))

    # Pass 2: Revalidate all successfully parsed artifacts with registry
    for file_path, attempt in all_artifacts:
        if attempt.artifact is None:
            domain_path = _path_to_domain_string(file_path, project)
            report.errors.extend(_convert_domain_errors_to_vault(
                attempt.report, domain_path
            ))
            continue

        domain_path = _path_to_domain_string(file_path, project)
        full_report = validate_artifact(attempt.artifact, registry)
        domain_errors = _convert_domain_errors_to_vault(full_report, domain_path)

        # Convert "not found in registry" FILE errors to doc-specific errors
        # when the missing file maps to a direct docs candidate that failed to parse
        if domain_errors:
            converted_errors: list[ValidationError] = []
            for err in domain_errors:
                if "not found in registry" in err.message:
                    for failed_alias in failed_doc_aliases:
                        if failed_alias in err.message:
                            doc_file_path = doc_candidate_lookup.get(failed_alias)
                            if doc_file_path is None:
                                doc_file_path = project.path / failed_alias
                            err_path = _path_to_domain_string(doc_file_path, project)
                            converted_err = ValidationError(
                                path=Path(doc_file_path),
                                line=None,
                                message=f"Referenced doc '{err_path}' does not match any supported doc artifact state",
                            )
                            converted_errors.append(converted_err)
                            break
                    else:
                        converted_errors.append(err)
                else:
                    converted_errors.append(err)
            domain_errors = converted_errors

        report.errors.extend(domain_errors)

    return report
