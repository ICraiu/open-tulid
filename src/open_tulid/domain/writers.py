from __future__ import annotations

import os

from .schema import (
    Artifact,
    ArtifactRegistry,
    ArtifactWriteResult,
    Field,
    FieldType,
    Section,
    ValidationReport,
)
from .validation import validate_artifact


def serialize_artifact_content(
    artifact: Artifact,
    registry: ArtifactRegistry | None = None,
) -> ArtifactWriteResult:
    report = ValidationReport()

    art_report = validate_artifact(artifact, registry)
    if not art_report.is_valid:
        report.errors.extend(art_report.errors)
        return ArtifactWriteResult(report=report)

    if not artifact.path or not artifact.path.strip():
        report.add_error("artifact.path", "Artifact path must be non-empty")
        return ArtifactWriteResult(report=report)

    lines: list[str] = []
    template_section_order = [s.name for s in artifact.template.sections]
    template_section_map: dict[str, Section] = {}
    for sec in artifact.sections:
        template_section_map[sec.name] = sec

    for sec_name in template_section_order:
        sec_tpl = None
        for s in artifact.template.sections:
            if s.name == sec_name:
                sec_tpl = s
                break

        if sec_tpl is not None and not sec_tpl.required and sec_name not in template_section_map:
            continue

        if sec_name not in template_section_map:
            continue

        sec = template_section_map[sec_name]
        lines.append(f"## {sec.name}")
        lines.append("")

        if sec_tpl is not None:
            field_order = [f.name for f in sec_tpl.fields]
            field_map: dict[str, Field] = {}
            for f in sec.fields:
                field_map[f.name] = f

            for fname in field_order:
                if fname in field_map:
                    f = field_map[fname]
                    lines.append(_format_field(f))

            for f in sec.fields:
                if f.name not in field_order:
                    lines.append(_format_field(f))
        else:
            for f in sec.fields:
                lines.append(_format_field(f))

        lines.append("")

    content = "\n".join(lines)
    return ArtifactWriteResult(path=artifact.path, content=content, report=report)


def _format_field(field: Field) -> str:
    if field.type == FieldType.FILE_LIST and isinstance(field.value, list):
        return f"{field.name}: {', '.join(field.value)}"
    return f"{field.name}: {field.value}"


def write_artifact_file(
    artifact: Artifact,
    path: str | None = None,
    registry: ArtifactRegistry | None = None,
) -> ArtifactWriteResult:
    write_result = serialize_artifact_content(artifact, registry)

    if not write_result.report.is_valid:
        return write_result

    target_path = path or artifact.path

    if not target_path or not target_path.strip():
        write_result.report.add_error("path", "No path provided and artifact has no path")
        return write_result

    parent = os.path.dirname(target_path)
    if parent and not os.path.exists(parent):
        write_result.report.add_error(
            "path",
            f"Parent directory does not exist: {parent}",
        )
        return write_result

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(write_result.content or "")
    except OSError as e:
        write_result.report.add_error("path", f"Cannot write file: {e}")
        return write_result

    write_result.path = target_path
    return write_result
