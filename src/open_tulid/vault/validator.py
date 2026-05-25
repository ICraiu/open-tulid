from __future__ import annotations

from pathlib import Path

from open_tulid.domain import WorkflowDefinition
from open_tulid.adapters import AdapterBuildRequest, build_storage_adapter, default_adapter_type
from open_tulid.models import Config, Project, ValidationError, ValidationReport
from open_tulid.runtime import TaskManager, ValidateProject
from open_tulid.vault.links import validate_kanban_file
from open_tulid.vault.project import iter_configured_projects
from open_tulid.workflow.runtime import load_workflow_definition


REQUIRED_DIRS = ["kanban", "docs", "tasks", "agents"]


def validate_project(
    project: Project,
    workflow_definition: WorkflowDefinition | None = None,
    tracker_type: str | None = None,
) -> ValidationReport:
    """Validate project structure, then workflow semantics when available."""
    report = ValidationReport()
    report.checked_projects += 1

    abs_project = project.path.resolve()

    for dir_name in REQUIRED_DIRS:
        dir_path = abs_project / dir_name
        if not dir_path.is_dir():
            report.errors.append(ValidationError(
                path=project.path,
                line=None,
                message=f"Project '{project.name}' is missing required directory: {dir_name}/",
            ))
    if not (abs_project / "workflow.yaml").is_file():
        report.errors.append(ValidationError(
            path=project.path,
            line=None,
            message=f"Project '{project.name}' is missing required file: workflow.yaml",
        ))

    # Validate kanban directory contents
    kanban_dir = abs_project / "kanban"
    if kanban_dir.is_dir():
        for child in sorted(kanban_dir.iterdir()):
            if child.is_dir():
                report.errors.append(ValidationError(
                    path=kanban_dir,
                    line=None,
                    message=f"Subdirectory found in kanban/: {child.name}/",
                ))
            elif not child.is_file():
                continue
            elif not child.name.endswith(".md"):
                report.errors.append(ValidationError(
                    path=child,
                    line=None,
                    message=f"Non-Markdown file in kanban/: {child.name}",
                ))
            else:
                kanban_report = validate_kanban_file(project, child)
                report.errors.extend(kanban_report.errors)
                report.checked_kanban_files += kanban_report.checked_kanban_files
                report.checked_task_links += kanban_report.checked_task_links

    if workflow_definition is not None:
        try:
            adapter = build_storage_adapter(AdapterBuildRequest(
                project_id=project.name,
                project_root=abs_project,
                tracker_type=tracker_type or default_adapter_type(),
                workflow=workflow_definition,
            ))
        except ValueError as exc:
            report.errors.append(ValidationError(
                path=project.path,
                line=None,
                message=f"workflow.runtime_unavailable: {exc}",
            ))
            return report
        runtime_result = TaskManager(
            workflow=workflow_definition,
            adapter=adapter,
        ).validate_project(ValidateProject(project_id=project.name))
        report.errors.extend(
            ValidationError(
                path=project.path,
                line=None,
                message=f"{error.code}: {error.message}",
            )
            for error in runtime_result.errors
        )

    return report


def validate_vault(
    config: Config,
    workflow_definition: WorkflowDefinition | None = None,
) -> ValidationReport:
    report = ValidationReport()
    projects = iter_configured_projects(config)

    for project in projects:
        # Check if project directory exists
        if not project.path.is_dir():
            report.errors.append(ValidationError(
                path=project.path,
                line=None,
                message=f"Configured project directory does not exist: {project.path}",
            ))
            report.checked_projects += 1
            continue

        selected_workflow = workflow_definition
        if selected_workflow is None:
            config_for_project = config.project_configs.get(project.name)
            workflow_path = config_for_project.workflow_path if config_for_project is not None else None
            if workflow_path is not None and workflow_path.is_file():
                loaded_workflow = load_workflow_definition(workflow_path)
                if loaded_workflow.valid:
                    selected_workflow = loaded_workflow.definition
                else:
                    report.errors.extend(
                        ValidationError(
                            path=workflow_path,
                            line=getattr(diagnostic, "line", None),
                            message=f"{diagnostic.code}: {diagnostic.message}",
                        )
                        for diagnostic in loaded_workflow.diagnostics
                    )
        project_report = validate_project(
            project,
            selected_workflow,
            tracker_type=config.tracker_type,
        )
        report.errors.extend(project_report.errors)
        report.checked_projects += project_report.checked_projects
        report.checked_kanban_files += project_report.checked_kanban_files
        report.checked_task_links += project_report.checked_task_links

    return report
