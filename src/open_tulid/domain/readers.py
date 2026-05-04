from __future__ import annotations

import os

from .schema import (
    Artifact,
    ArtifactReadResult,
    ArtifactRegistry,
    ArtifactState,
    Field,
    FieldType,
    Section,
    Template,
    ValidationReport,
)
from .validation import validate_artifact


def parse_artifact_content(
    path: str,
    content: str,
    state: ArtifactState,
    template: Template,
    registry: ArtifactRegistry | None = None,
    validate_links: bool = True,
) -> ArtifactReadResult:
    report = ValidationReport()

    if not path or not path.strip():
        report.add_error("path", "Path must be non-empty")
        return ArtifactReadResult(report=report)

    if state != template.state:
        report.add_error(
            "state",
            f"Requested state {state.value} does not match template state {template.state.value}",
        )
        return ArtifactReadResult(report=report)

    lines = content.split("\n")
    sections: list[Section] = []
    section_names_seen: set[str] = set()
    current_section: Section | None = None
    found_first_section = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("## "):
            section_name = stripped[3:].strip()

            found_first_section = True

            if section_name in section_names_seen:
                report.add_error(
                    f"section.{section_name}",
                    f"Duplicate section '{section_name}'",
                )
            section_names_seen.add(section_name)

            if current_section is not None:
                sections.append(current_section)

            current_section = Section(name=section_name, fields=[])
            continue

        if stripped == "":
            continue

        if not found_first_section:
            report.add_error(
                "preamble",
                "Non-empty text before the first section is invalid",
            )
            return ArtifactReadResult(report=report)

        if current_section is None:
            continue

        if ":" in stripped:
            colon_idx = stripped.index(":")
            field_name = stripped[:colon_idx].strip()
            field_value = stripped[colon_idx + 1 :].strip()

            existing_field = None
            for f in current_section.fields:
                if f.name == field_name:
                    existing_field = f
                    break

            if existing_field is not None:
                report.add_error(
                    f"section.{current_section.name}.field.{field_name}",
                    f"Duplicate field '{field_name}' in section '{current_section.name}'",
                )
                continue

            field_type = _infer_field_type(field_name, field_value, template)
            parsed_value: str | list[str] = field_value
            if field_type == FieldType.FILE_LIST:
                parsed_value = [v.strip() for v in field_value.split(",") if v.strip()]
            current_section.fields.append(
                Field(name=field_name, type=field_type, value=parsed_value)
            )

    if current_section is not None:
        sections.append(current_section)

    artifact = Artifact(
        path=path,
        state=state,
        template=template,
        sections=sections,
    )

    art_report = validate_artifact(artifact, registry, validate_links=validate_links)
    report.errors.extend(art_report.errors)

    if report.is_valid:
        return ArtifactReadResult(artifact=artifact, report=report)
    else:
        return ArtifactReadResult(report=report)


def _infer_field_type(field_name: str, field_value: str, template: Template) -> FieldType:
    for sec_tpl in template.sections:
        for fld_tpl in sec_tpl.fields:
            if fld_tpl.name == field_name:
                return fld_tpl.type

    return FieldType.STRING


def read_artifact_file(
    path: str,
    state: ArtifactState,
    template: Template,
    registry: ArtifactRegistry | None = None,
    validate_links: bool = True,
    domain_path: str | None = None,
) -> ArtifactReadResult:
    report = ValidationReport()

    effective_path = domain_path if domain_path else path

    if not effective_path or not effective_path.strip():
        report.add_error("path", "Path must be non-empty")
        return ArtifactReadResult(report=report)

    if not os.path.exists(path):
        report.add_error("path", f"File not found: {path}")
        return ArtifactReadResult(report=report)

    if state != template.state:
        report.add_error(
            "state",
            f"Requested state {state.value} does not match template state {template.state.value}",
        )
        return ArtifactReadResult(report=report)

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        report.add_error("path", f"File is not valid UTF-8: {path}")
        return ArtifactReadResult(report=report)
    except OSError as e:
        report.add_error("path", f"Cannot read file: {e}")
        return ArtifactReadResult(report=report)

    return parse_artifact_content(effective_path, content, state, template, registry, validate_links=validate_links)


def parse_artifact_content_no_links(
    path: str,
    content: str,
    state: ArtifactState,
    template: Template,
) -> ArtifactReadResult:
    return parse_artifact_content(path, content, state, template, validate_links=False)
