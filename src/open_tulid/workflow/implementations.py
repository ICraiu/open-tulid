from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from open_tulid.adapters.base import StorageAdapter
from open_tulid.domain import DomainError, ProjectSnapshot, Task


CommandRunner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


class OperationLogger(Protocol):
    def operation_started(
        self,
        context: "WorkflowExecutionContext",
        operation: str,
        args: Mapping[str, Any],
    ) -> None: ...

    def operation_finished(
        self,
        context: "WorkflowExecutionContext",
        operation: str,
        args: Mapping[str, Any],
        result: "OperationResult",
    ) -> None: ...


@dataclass(frozen=True)
class WorkflowExecutionContext:
    project_id: str | None = None
    project_root: Path | None = None
    vault_root: Path | None = None
    adapter: StorageAdapter | None = None
    snapshot: ProjectSnapshot | None = None
    task: Task | None = None
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    command_runner: CommandRunner | None = None
    approved_destructive: bool = False
    actor_type: str = "system"
    actor_id: str = "workflow-runtime"
    correlation_id: str | None = None
    transition_id: str | None = None
    operation_logger: OperationLogger | None = None


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    code: str
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationResult:
    accepted: bool
    code: str
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[DomainError, ...] = ()


def project_build(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    return _command_validation(
        "project_build",
        context,
        _command_arg(kwargs, default=("python", "-m", "pytest")),
    )


def git_status_clean(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    result = _run_command(context, ("git", "status", "--porcelain"))
    if result.returncode != 0:
        return _validation(False, "git_status_clean", result.stderr.strip() or "git status failed")
    if result.stdout.strip():
        return _validation(False, "git_status_clean", "Git working tree is not clean")
    return _validation(True, "git_status_clean", "Git working tree is clean")


def file_exists(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    path = _path_arg(context, kwargs, "path", fallback_keys=("file", "artifact"))
    if path is None:
        return _validation(False, "file_exists", "path is required")
    return _validation(path.exists(), "file_exists", f"File exists: {path}" if path.exists() else f"File not found: {path}")


def artifact_in_vault(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    path = _path_arg(context, kwargs, "path", fallback_keys=("artifact",))
    root = context.vault_root or context.project_root
    if path is None:
        return _validation(False, "artifact_in_vault", "path is required")
    if root is None:
        return _validation(False, "artifact_in_vault", "vault_root or project_root is required")
    in_root = _is_relative_to(path.resolve(), root.resolve())
    return _validation(in_root and path.exists(), "artifact_in_vault", f"Artifact is in vault: {path}" if in_root else f"Artifact is outside vault: {path}")


def artifact_link_in_vault(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    link = _first_string(kwargs, "link", "artifact", "path")
    if not link:
        return _validation(False, "artifact_link_in_vault", "link is required")
    resolved = _resolve_link(context, link)
    if resolved is None:
        return _validation(False, "artifact_link_in_vault", f"Cannot resolve link: {link}")
    root = context.vault_root or context.project_root
    if root is None:
        return _validation(False, "artifact_link_in_vault", "vault_root or project_root is required")
    passed = _is_relative_to(resolved.resolve(), root.resolve()) and resolved.exists()
    return _validation(passed, "artifact_link_in_vault", f"Resolved link: {resolved}")


def artifact_matches_template(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    content = _content_arg(context, kwargs)
    sections = _string_list(kwargs.get("sections") or kwargs.get("required_sections"))
    if content is None:
        return _validation(False, "artifact_matches_template", "content or path is required")
    missing = _missing_sections(content, sections)
    return _validation(not missing, "artifact_matches_template", f"Missing sections: {', '.join(missing)}" if missing else "Artifact matches template")


def template_sections_present(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    content = _content_arg(context, kwargs)
    sections = _string_list(kwargs.get("sections") or kwargs.get("required_sections"))
    if content is None:
        return _validation(False, "template_sections_present", "content or path is required")
    missing = _missing_sections(content, sections)
    return _validation(not missing, "template_sections_present", f"Missing sections: {', '.join(missing)}" if missing else "Required sections present")


def template_required_fields_present(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    content = _content_arg(context, kwargs)
    fields = _string_list(kwargs.get("fields") or kwargs.get("required_fields"))
    if content is None:
        return _validation(False, "template_required_fields_present", "content or path is required")
    missing = [field for field in fields if not _field_has_value(content, field)]
    return _validation(not missing, "template_required_fields_present", f"Missing fields: {', '.join(missing)}" if missing else "Required fields present")


def artifact_has_required_text(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    content = _content_arg(context, kwargs)
    required_text = _first_string(kwargs, "text", "required_text", "contains")
    if content is None:
        return _validation(False, "artifact_has_required_text", "content or path is required")
    if required_text:
        passed = required_text in content
        return _validation(passed, "artifact_has_required_text", f"Required text not found: {required_text}" if not passed else "Required text found")
    return _validation(bool(content.strip()), "artifact_has_required_text", "Artifact text is non-empty" if content.strip() else "Artifact text is empty")


def artifacts_match_template(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    pattern = _first_string(kwargs, "pattern", "glob", "path")
    if not pattern:
        return _validation(False, "artifacts_match_template", "pattern is required")
    root = context.project_root or context.vault_root
    if root is None:
        return _validation(False, "artifacts_match_template", "project_root or vault_root is required")
    matches = sorted(path for path in root.glob(pattern) if path.is_file())
    if not matches:
        return _validation(False, "artifacts_match_template", f"No files matched pattern: {pattern}")

    sections = _string_list(kwargs.get("sections") or kwargs.get("required_sections"))
    fields = _string_list(kwargs.get("fields") or kwargs.get("required_fields"))
    required_texts = _string_list(kwargs.get("texts") or kwargs.get("required_texts"))
    problems: list[str] = []

    for path in matches:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path.relative_to(root)} unreadable: {exc}")
            continue
        missing_sections = _missing_sections(content, sections)
        if missing_sections:
            problems.append(f"{path.relative_to(root)} missing sections: {', '.join(missing_sections)}")
        missing_fields = [field for field in fields if not _field_has_value(content, field)]
        if missing_fields:
            problems.append(f"{path.relative_to(root)} missing fields: {', '.join(missing_fields)}")
        missing_texts = [text for text in required_texts if text not in content]
        if missing_texts:
            problems.append(f"{path.relative_to(root)} missing text: {', '.join(missing_texts)}")

    if problems:
        return _validation(False, "artifacts_match_template", "; ".join(problems))
    return _validation(True, "artifacts_match_template", f"All files matched required template checks for pattern: {pattern}")


def branch_exists(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    branch = _first_string(kwargs, "branch", "name")
    if not branch:
        return _validation(False, "branch_exists", "branch is required")
    result = _run_command(context, ("git", "rev-parse", "--verify", branch))
    return _validation(result.returncode == 0, "branch_exists", f"Branch exists: {branch}" if result.returncode == 0 else f"Branch not found: {branch}")


def tests_pass(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    return _command_validation(
        "tests_pass",
        context,
        _command_arg(kwargs, default=("python", "-m", "pytest")),
    )


def link_target_exists(context: WorkflowExecutionContext, **kwargs: Any) -> ValidationResult:
    link = _first_string(kwargs, "link", "target", "path")
    if not link:
        return _validation(False, "link_target_exists", "link is required")
    resolved = _resolve_link(context, link)
    passed = resolved is not None and resolved.exists()
    return _validation(passed, "link_target_exists", f"Link target exists: {resolved}" if passed else f"Link target not found: {link}")


def move_task(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    if context.adapter is None:
        return _operation(False, "move_task", "adapter is required")
    task_id = _first_string(kwargs, "task_id") or (context.task.id if context.task else None)
    state = _first_string(kwargs, "to", "state", "target_state")
    if not task_id or not state:
        return _operation(False, "move_task", "task_id and target state are required")
    result = context.adapter.move_task(task_id, state)
    return _adapter_operation("move_task", result)


def copy_file(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    source = _path_arg(context, kwargs, "source", fallback_keys=("from",))
    target = _path_arg(context, kwargs, "target", fallback_keys=("to",))
    if source is None or target is None:
        return _operation(False, "copy_file", "source and target are required")
    if not source.is_file():
        return _operation(False, "copy_file", f"Source file not found: {source}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError as exc:
        return _operation(False, "copy_file", f"Cannot copy file: {exc}")
    return _operation(True, "copy_file", f"Copied {source} to {target}", {"path": str(target)})


def copy_field(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    source = kwargs.get("source")
    target = kwargs.get("target")
    source_field = _first_string(kwargs, "source_field", "from_field", "field")
    target_field = _first_string(kwargs, "target_field", "to_field") or source_field
    if not isinstance(source, Mapping) or not isinstance(target, dict) or not source_field or not target_field:
        return _operation(False, "copy_field", "source mapping, target mapping, and field are required")
    target[target_field] = source.get(source_field)
    return _operation(True, "copy_field", f"Copied field {source_field} to {target_field}", {"target": target})


def set_field(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    target = kwargs.get("target")
    field_name = _first_string(kwargs, "field", "name")
    if not isinstance(target, dict) or not field_name:
        return _operation(False, "set_field", "target mapping and field are required")
    target[field_name] = kwargs.get("value", "")
    return _operation(True, "set_field", f"Set field {field_name}", {"target": target})


def link_artifact(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    if context.adapter is None or context.task is None:
        return _operation(False, "link_artifact", "adapter and task are required")
    artifact = _first_string(kwargs, "artifact", "path", "link")
    if not artifact:
        return _operation(False, "link_artifact", "artifact is required")
    links = tuple(dict.fromkeys((*context.task.artifact_links, artifact)))
    updated = Task(
        id=context.task.id,
        title=context.task.title,
        path=context.task.path,
        current_state=context.task.current_state,
        task_type=context.task.task_type,
        dependencies=context.task.dependencies,
        artifact_links=links,
        parent_id=context.task.parent_id,
        metadata=context.task.metadata,
        body=context.task.body,
    )
    return _adapter_operation("link_artifact", context.adapter.write_task(updated))


def git_add(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    paths = _string_list(kwargs.get("paths") or kwargs.get("path") or ".")
    return _command_operation("git_add", context, ("git", "add", *paths))


def git_commit(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    message = _first_string(kwargs, "message")
    if not message:
        return _operation(False, "git_commit", "message is required")
    return _command_operation("git_commit", context, ("git", "commit", "-m", message))


def git_reset_hard(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    if not (context.approved_destructive or kwargs.get("approved") is True):
        return _operation(False, "git_reset_hard", "destructive operation requires approval")
    target = _first_string(kwargs, "target", "ref") or "HEAD"
    return _command_operation("git_reset_hard", context, ("git", "reset", "--hard", target))


def create_branch(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    branch = _first_string(kwargs, "branch", "name")
    if not branch:
        return _operation(False, "create_branch", "branch is required")
    return _command_operation("create_branch", context, ("git", "branch", branch))


def checkout_branch(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    branch = _first_string(kwargs, "branch", "name")
    if not branch:
        return _operation(False, "checkout_branch", "branch is required")
    create = kwargs.get("create", False) is True
    command = ("git", "checkout", "-b", branch) if create else ("git", "checkout", branch)
    return _command_operation("checkout_branch", context, command)


def write_file(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    path = _path_arg(context, kwargs, "path")
    if path is None:
        return _operation(False, "write_file", "path is required")
    content = str(kwargs.get("content", ""))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return _operation(False, "write_file", f"Cannot write file: {exc}")
    return _operation(True, "write_file", f"Wrote {path}", {"path": str(path)})


def append_event(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    event = kwargs.get("event") or kwargs
    if not isinstance(event, Mapping):
        return _operation(False, "append_event", "event mapping is required")
    if context.adapter is not None:
        return _adapter_operation("append_event", context.adapter.append_event(event))
    path = _path_arg(context, kwargs, "path")
    if path is None:
        root = context.project_root
        if root is None:
            return _operation(False, "append_event", "adapter or project_root is required")
        path = root / "events" / f"{str(event.get('timestamp', 'undated'))[:10]}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(event), sort_keys=True, separators=(",", ":")) + "\n")
    except OSError as exc:
        return _operation(False, "append_event", f"Cannot append event: {exc}")
    return _operation(True, "append_event", f"Appended event to {path}", {"path": str(path)})


def update_kanban_view(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
    return move_task(context, **kwargs)


VALIDATION_IMPLEMENTATIONS = {
    "project_build": project_build,
    "git_status_clean": git_status_clean,
    "file_exists": file_exists,
    "artifact_in_vault": artifact_in_vault,
    "artifact_link_in_vault": artifact_link_in_vault,
    "artifact_matches_template": artifact_matches_template,
    "template_sections_present": template_sections_present,
    "template_required_fields_present": template_required_fields_present,
    "artifact_has_required_text": artifact_has_required_text,
    "artifacts_match_template": artifacts_match_template,
    "branch_exists": branch_exists,
    "tests_pass": tests_pass,
    "link_target_exists": link_target_exists,
}


def _logged(operation: str, fn: Callable[..., OperationResult]) -> Callable[..., OperationResult]:
    def wrapped(context: WorkflowExecutionContext, **kwargs: Any) -> OperationResult:
        logger = context.operation_logger
        if logger is not None:
            logger.operation_started(context, operation, kwargs)
        result = fn(context, **kwargs)
        if logger is not None:
            logger.operation_finished(context, operation, kwargs, result)
        return result
    return wrapped


OPERATION_IMPLEMENTATIONS = {
    "move_task": _logged("move_task", move_task),
    "copy_file": _logged("copy_file", copy_file),
    "copy_field": _logged("copy_field", copy_field),
    "set_field": _logged("set_field", set_field),
    "link_artifact": _logged("link_artifact", link_artifact),
    "git_add": _logged("git_add", git_add),
    "git_commit": _logged("git_commit", git_commit),
    "git_reset_hard": _logged("git_reset_hard", git_reset_hard),
    "create_branch": _logged("create_branch", create_branch),
    "checkout_branch": _logged("checkout_branch", checkout_branch),
    "write_file": _logged("write_file", write_file),
    "append_event": _logged("append_event", append_event),
    "update_kanban_view": _logged("update_kanban_view", update_kanban_view),
}


def _validation(passed: bool, code: str, message: str = "", data: Mapping[str, Any] | None = None) -> ValidationResult:
    return ValidationResult(passed=passed, code=code, message=message, data=data or {})


def _operation(
    accepted: bool,
    code: str,
    message: str = "",
    data: Mapping[str, Any] | None = None,
    errors: tuple[DomainError, ...] = (),
) -> OperationResult:
    return OperationResult(accepted=accepted, code=code, message=message, data=data or {}, errors=errors)


def _adapter_operation(code: str, result: Any) -> OperationResult:
    errors = tuple(getattr(result, "errors", ()))
    return _operation(not errors, code, getattr(result, "path", None) or "", errors=errors)


def _command_validation(code: str, context: WorkflowExecutionContext, command: Sequence[str]) -> ValidationResult:
    result = _run_command(context, command)
    return _validation(result.returncode == 0, code, (result.stderr or result.stdout).strip())


def _command_operation(code: str, context: WorkflowExecutionContext, command: Sequence[str]) -> OperationResult:
    result = _run_command(context, command)
    return _operation(result.returncode == 0, code, (result.stderr or result.stdout).strip())


def _run_command(context: WorkflowExecutionContext, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if context.command_runner is not None:
        return context.command_runner(tuple(command), context.project_root)
    return subprocess.run(
        tuple(command),
        cwd=context.project_root,
        text=True,
        capture_output=True,
        check=False,
    )


def _command_arg(kwargs: Mapping[str, Any], default: Sequence[str]) -> Sequence[str]:
    command = kwargs.get("command")
    if command is None:
        return tuple(default)
    if isinstance(command, str):
        return tuple(command.split())
    if isinstance(command, Sequence):
        return tuple(str(part) for part in command)
    return tuple(default)


def _path_arg(context: WorkflowExecutionContext, kwargs: Mapping[str, Any], key: str, fallback_keys: tuple[str, ...] = ()) -> Path | None:
    value = kwargs.get(key)
    for fallback in fallback_keys:
        if value is None:
            value = kwargs.get(fallback)
    if value is None:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    root = context.project_root or context.vault_root
    return root / path if root is not None else path


def _content_arg(context: WorkflowExecutionContext, kwargs: Mapping[str, Any]) -> str | None:
    content = kwargs.get("content")
    if content is not None:
        return str(content)
    path = _path_arg(context, kwargs, "path", fallback_keys=("artifact", "file"))
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _resolve_link(context: WorkflowExecutionContext, link: str) -> Path | None:
    wiki = re.fullmatch(r"\[\[([^\]|#/\\]+)\]\]", link.strip())
    target = wiki.group(1) if wiki else link
    path = Path(target)
    roots = [root for root in (context.project_root, context.vault_root) if root is not None]
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    for root in roots:
        candidates.append(root / path)
        candidates.append(root / "tasks" / f"{target}.md")
        candidates.append(root / "docs" / f"{target}.md")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def _first_string(kwargs: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = kwargs.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    return [str(value)]


def _missing_sections(content: str, sections: list[str]) -> list[str]:
    existing = {line.strip()[3:].strip() for line in content.splitlines() if line.strip().startswith("## ")}
    return [section for section in sections if section not in existing]


def _field_has_value(content: str, field: str) -> bool:
    prefix = f"{field}:"
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return bool(stripped[len(prefix):].strip())
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
