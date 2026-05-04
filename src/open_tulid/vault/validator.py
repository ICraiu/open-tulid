from __future__ import annotations

from pathlib import Path

from open_tulid.config import Config
from open_tulid.models import Project, ValidationError, ValidationReport
from open_tulid.vault.domain_integration import validate_project_domain_artifacts
from open_tulid.vault.links import validate_kanban_file
from open_tulid.vault.project import iter_configured_projects


REQUIRED_DIRS = ["kanban", "docs", "tasks"]


def validate_project(project: Project) -> ValidationReport:
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

    domain_report = validate_project_domain_artifacts(project)
    report.errors.extend(domain_report.errors)

    return report


def validate_vault(config: Config) -> ValidationReport:
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

        project_report = validate_project(project)
        report.errors.extend(project_report.errors)
        report.checked_projects += project_report.checked_projects
        report.checked_kanban_files += project_report.checked_kanban_files
        report.checked_task_links += project_report.checked_task_links

    return report
