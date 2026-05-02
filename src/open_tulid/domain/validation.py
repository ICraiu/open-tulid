from __future__ import annotations

from .schema import (
    Artifact,
    ArtifactRegistry,
    ArtifactState,
    COMPATIBLE_VALIDATORS,
    Field,
    FieldType,
    FieldTemplate,
    MappingRule,
    MappingRuleType,
    Section,
    SectionTemplate,
    Template,
    Transition,
    ValidationReport,
    ValidatorType,
)


def validate_template(template: Template) -> ValidationReport:
    report = ValidationReport()

    if not template.name or not template.name.strip():
        report.add_error("template.name", "Template name must be non-empty", path=template.name or None)

    if not isinstance(template.state, ArtifactState):
        report.add_error("template.state", "Template state must be a valid ArtifactState")

    section_names: set[str] = set()
    for i, sec_tpl in enumerate(template.sections):
        loc = f"sections[{i}]"

        if not sec_tpl.name or not sec_tpl.name.strip():
            report.add_error(f"{loc}.name", "SectionTemplate name must be non-empty")

        if sec_tpl.name in section_names:
            report.add_error(f"{loc}.name", f"Duplicate section name: {sec_tpl.name}")
        section_names.add(sec_tpl.name)

        field_names: set[str] = set()
        for j, fld_tpl in enumerate(sec_tpl.fields):
            fld_loc = f"{loc}.fields[{j}]"

            if not fld_tpl.name or not fld_tpl.name.strip():
                report.add_error(f"{fld_loc}.name", "FieldTemplate name must be non-empty")

            if fld_tpl.name in field_names:
                report.add_error(f"{fld_loc}.name", f"Duplicate field name in section '{sec_tpl.name}': {fld_tpl.name}")
            field_names.add(fld_tpl.name)

            if not isinstance(fld_tpl.type, FieldType):
                report.add_error(f"{fld_loc}.type", "Field type must be a valid FieldType")

            compatible = COMPATIBLE_VALIDATORS.get(fld_tpl.type, set())
            for v in fld_tpl.validators:
                if v not in compatible:
                    report.add_error(
                        f"{fld_loc}.validators",
                        f"Validator {v.value} is not compatible with type {fld_tpl.type.value}",
                    )

    return report


def _find_field_in_sections(sections: list[Section], field_name: str) -> Field | None:
    for section in sections:
        for f in section.fields:
            if f.name == field_name:
                return f
    return None


def _find_fields_in_sections(sections: list[Section], field_name: str) -> list[Field]:
    matches: list[Field] = []
    for section in sections:
        for f in section.fields:
            if f.name == field_name:
                matches.append(f)
    return matches


def _find_field_template(
    section_tpl: SectionTemplate, field_name: str
) -> FieldTemplate | None:
    for f in section_tpl.fields:
        if f.name == field_name:
            return f
    return None


def _is_field_required(
    field_tpl: FieldTemplate,
    artifact: Artifact,
    report: ValidationReport,
    location: str,
) -> bool:
    if field_tpl.required_when is None:
        return field_tpl.required

    matches = _find_fields_in_sections(artifact.sections, field_tpl.required_when.field_name)
    if not matches:
        report.add_error(
            location,
            f"Cannot evaluate required_when for '{field_tpl.name}': source field "
            f"'{field_tpl.required_when.field_name}' is missing",
        )
        return False
    if len(matches) > 1:
        report.add_error(
            location,
            f"Cannot evaluate required_when for '{field_tpl.name}': source field "
            f"'{field_tpl.required_when.field_name}' is ambiguous",
        )
        return False

    return str(matches[0].value) == field_tpl.required_when.equals


def _has_proof_validator(template: Template) -> bool:
    for sec_tpl in template.sections:
        for f in sec_tpl.fields:
            if ValidatorType.TASK_HAS_PROOF_WHEN_DONE in f.validators:
                return True
    return False


def _validate_field_value_type(field: Field, field_tpl: FieldTemplate | None) -> ValidationReport:
    report = ValidationReport()

    if field_tpl and field.type != field_tpl.type:
        report.add_error(
            f"field.{field.name}",
            f"Field type {field.type.value} does not match template type {field_tpl.type.value}",
        )
        return report

    if field.type in (FieldType.STRING, FieldType.STATUS):
        if not isinstance(field.value, str):
            report.add_error(
                f"field.{field.name}",
                f"{field.type.value} field must have a string value, got {type(field.value).__name__}",
            )
    elif field.type == FieldType.FILE:
        if not isinstance(field.value, str):
            report.add_error(
                f"field.{field.name}",
                f"FILE field must have a string value, got {type(field.value).__name__}",
            )
    elif field.type == FieldType.FILE_LIST:
        if not isinstance(field.value, list):
            report.add_error(
                f"field.{field.name}",
                f"FILE_LIST field must have a list value, got {type(field.value).__name__}",
            )

    return report


def _validate_field_validators(
    field: Field,
    field_tpl: FieldTemplate | None,
    artifact: Artifact,
    registry: ArtifactRegistry | None,
) -> ValidationReport:
    report = ValidationReport()

    if field_tpl is None:
        return report

    for v in field_tpl.validators:
        if v == ValidatorType.NON_EMPTY_TEXT:
            if isinstance(field.value, str):
                if not field.value.strip():
                    report.add_error(
                        f"field.{field.name}",
                        f"Field '{field.name}' must contain non-empty text",
                    )
            elif isinstance(field.value, list):
                if not any(v.strip() for v in field.value):
                    report.add_error(
                        f"field.{field.name}",
                        f"Field '{field.name}' must contain at least one non-empty value",
                    )

        elif v == ValidatorType.FILE_LINK_EXISTS:
            if registry is None:
                report.add_error(
                    f"field.{field.name}",
                    "File link validation requires an artifact registry",
                )
                continue
            paths: list[str] = []
            if isinstance(field.value, str):
                paths = [field.value]
            elif isinstance(field.value, list):
                paths = field.value
            for p in paths:
                if not registry.contains(p):
                    report.add_error(
                        f"field.{field.name}",
                        f"File link '{p}' not found in registry",
                    )

        elif v == ValidatorType.FILE_LINK_MATCHES_TEMPLATE:
            if registry is None:
                report.add_error(
                    f"field.{field.name}",
                    "File link template validation requires an artifact registry",
                )
                continue
            paths: list[str] = []
            if isinstance(field.value, str):
                paths = [field.value]
            elif isinstance(field.value, list):
                paths = field.value
            for p in paths:
                linked = registry.get(p)
                if linked is None:
                    report.add_error(
                        f"field.{field.name}",
                        f"Linked artifact '{p}' not found in registry",
                    )
                else:
                    linked_report = validate_artifact(linked, registry)
                    if not linked_report.is_valid:
                        report.add_error(
                            f"field.{field.name}",
                            f"Linked artifact '{p}' does not validate against its template",
                        )

    return report


def validate_artifact(
    artifact: Artifact,
    registry: ArtifactRegistry | None = None,
) -> ValidationReport:
    report = ValidationReport()

    if not artifact.path or not artifact.path.strip():
        report.add_error("artifact.path", "Artifact path must be non-empty")

    if artifact.state != artifact.template.state:
        report.add_error(
            "artifact.state",
            f"Artifact state {artifact.state.value} does not match template state {artifact.template.state.value}",
        )

    template_section_map: dict[str, SectionTemplate] = {}
    for sec_tpl in artifact.template.sections:
        template_section_map[sec_tpl.name] = sec_tpl

    artifact_section_map: dict[str, Section] = {}
    for sec in artifact.sections:
        sec_loc = f"section.{sec.name or '<empty>'}"
        if not sec.name or not sec.name.strip():
            report.add_error(f"{sec_loc}.name", "Section name must be non-empty")
        if sec.name in artifact_section_map:
            report.add_error(
                f"section.{sec.name}",
                f"Duplicate section '{sec.name}' in artifact",
            )
        artifact_section_map[sec.name] = sec

        seen_fields: set[str] = set()
        for f in sec.fields:
            if not f.name or not f.name.strip():
                report.add_error(
                    f"{sec_loc}.field",
                    "Field name must be non-empty",
                )
            if f.name in seen_fields:
                report.add_error(
                    f"{sec_loc}.field.{f.name}",
                    f"Duplicate field '{f.name}' in section '{sec.name}'",
                )
            seen_fields.add(f.name)
            type_report = _validate_field_value_type(f, None)
            report.errors.extend(type_report.errors)

    for sec_name, sec_tpl in template_section_map.items():
        sec_loc = f"section.{sec_name}"

        if sec_tpl.required and sec_name not in artifact_section_map:
            report.add_error(
                sec_loc,
                f"Required section '{sec_name}' is missing from artifact",
            )
            continue

        if sec_name not in artifact_section_map:
            continue

        artifact_sec = artifact_section_map[sec_name]

        for field_tpl in sec_tpl.fields:
            artifact_field = _find_field_in_sections([artifact_sec], field_tpl.name)
            field_required = _is_field_required(
                field_tpl,
                artifact,
                report,
                f"{sec_loc}.field.{field_tpl.name}",
            )

            if artifact_field is not None:
                type_report = _validate_field_value_type(artifact_field, field_tpl)
                report.errors.extend(type_report.errors)
                val_report = _validate_field_validators(
                    artifact_field, field_tpl, artifact, registry
                )
                report.errors.extend(val_report.errors)
            elif field_required:
                report.add_error(
                    f"{sec_loc}.field.{field_tpl.name}",
                    f"Required field '{field_tpl.name}' is missing",
                )

    has_proof_validator = _has_proof_validator(artifact.template)

    if has_proof_validator:
        status_field = _find_field_in_sections(artifact.sections, "Status")
        if status_field is not None and str(status_field.value) == "done":
            proof_sec = None
            for sec in artifact.sections:
                if sec.name == "Proof":
                    proof_sec = sec
                    break

            if proof_sec is None:
                report.add_error(
                    "section.Proof",
                    "CompletedTask with Status=done must have a Proof section",
                )
            else:
                evidence_field = None
                changed_files_field = None
                validation_result_field = None

                for f in proof_sec.fields:
                    if f.name == "Evidence":
                        evidence_field = f
                    elif f.name == "Changed files":
                        changed_files_field = f
                    elif f.name == "Validation result":
                        validation_result_field = f

                if evidence_field is not None:
                    if isinstance(evidence_field.value, str) and not evidence_field.value.strip():
                        report.add_error(
                            "section.Proof.field.Evidence",
                            "CompletedTask with Status=done must have non-empty Evidence",
                        )
                    elif isinstance(evidence_field.value, list) and not any(
                        v.strip() for v in evidence_field.value
                    ):
                        report.add_error(
                            "section.Proof.field.Evidence",
                            "CompletedTask with Status=done must have non-empty Evidence",
                        )

                if changed_files_field is not None:
                    if isinstance(changed_files_field.value, list):
                        if not changed_files_field.value:
                            report.add_error(
                                "section.Proof.field.Changed files",
                                "CompletedTask with Status=done must have non-empty Changed files",
                            )
                    elif isinstance(changed_files_field.value, str):
                        if not changed_files_field.value.strip():
                            report.add_error(
                                "section.Proof.field.Changed files",
                                "CompletedTask with Status=done must have non-empty Changed files",
                            )

                if validation_result_field is not None:
                    if isinstance(validation_result_field.value, str) and not validation_result_field.value.strip():
                        report.add_error(
                            "section.Proof.field.Validation result",
                            "CompletedTask with Status=done must have non-empty Validation result",
                        )

    return report


def validate_transition(transition: Transition) -> ValidationReport:
    report = ValidationReport()

    if not transition.name or not transition.name.strip():
        report.add_error("transition.name", "Transition name must be non-empty")

    if transition.from_state == transition.to_state:
        report.add_error(
            "transition.states",
            "from_state and to_state must be different",
        )

    if not transition.required_inputs:
        report.add_error(
            "transition.required_inputs",
            "Transition must have at least one required input",
        )

    if transition.output_template is None:
        report.add_error(
            "transition.output_template",
            "Transition must have exactly one output_template",
        )
    else:
        if transition.output_template.state != transition.to_state:
            report.add_error(
                "transition.output_template.state",
                f"output_template state {transition.output_template.state.value} "
                f"does not match to_state {transition.to_state.value}",
            )

    from_state_found = False
    for inp in transition.required_inputs:
        if inp.state == transition.from_state:
            from_state_found = True
            break

    if not from_state_found:
        report.add_error(
            "transition.required_inputs",
            f"No required input matches from_state {transition.from_state.value}",
        )

    if (
        not transition.mapping_rules
        and transition.output_template is not None
        and _template_requires_output_content(transition.output_template)
    ):
        report.add_error(
            "transition.mapping_rules",
            "Transition mapping_rules cannot be empty when the output template has required content",
        )

    for i, mr in enumerate(transition.mapping_rules):
        mr_loc = f"mapping_rules[{i}]"

        if mr.kind == MappingRuleType.CARRY_FIELD:
            if not mr.from_section or not mr.from_field:
                report.add_error(
                    mr_loc,
                    "CARRY_FIELD mapping must have from_section and from_field",
                )
            if not mr.to_section or not mr.to_field:
                report.add_error(
                    mr_loc,
                    "CARRY_FIELD mapping must have to_section and to_field",
                )

        elif mr.kind == MappingRuleType.SET_FIELD:
            if not mr.to_section or not mr.to_field:
                report.add_error(
                    mr_loc,
                    "SET_FIELD mapping must have to_section and to_field",
                )
            if not mr.value:
                report.add_error(
                    mr_loc,
                    "SET_FIELD mapping must have a value",
                )

        elif mr.kind == MappingRuleType.LINK_ARTIFACT:
            if not mr.from_section or not mr.from_field:
                report.add_error(
                    mr_loc,
                    "LINK_ARTIFACT mapping must have from_section and from_field",
                )
            if not mr.to_section or not mr.to_field:
                report.add_error(
                    mr_loc,
                    "LINK_ARTIFACT mapping must have to_section and to_field",
                )
            if transition.output_template is not None:
                target_field_tpl = None
                for sec_tpl in transition.output_template.sections:
                    if sec_tpl.name == mr.to_section:
                        target_field_tpl = _find_field_template(sec_tpl, mr.to_field)
                        break
                if target_field_tpl is not None and target_field_tpl.type not in (
                    FieldType.FILE,
                    FieldType.FILE_LIST,
                ):
                    report.add_error(
                        mr_loc,
                        f"LINK_ARTIFACT mapping targets non-file field '{mr.to_field}' of type {target_field_tpl.type.value}",
                    )

        elif mr.kind == MappingRuleType.CREATE_SECTION:
            if not mr.to_section:
                report.add_error(
                    mr_loc,
                    "CREATE_SECTION mapping must have to_section",
                )

    return report


def _template_requires_output_content(template: Template) -> bool:
    for sec_tpl in template.sections:
        if sec_tpl.required:
            return True
        for field_tpl in sec_tpl.fields:
            if field_tpl.required or field_tpl.required_when is not None:
                return True
    return False
