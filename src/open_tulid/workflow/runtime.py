from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import workflow_engine

from open_tulid.domain import WorkflowDefinition
from open_tulid.workflow.compiler import compile_workflow
from open_tulid.workflow.diagnostics import WorkflowCompileDiagnostic


@dataclass(frozen=True)
class WorkflowLoadResult:
    definition: WorkflowDefinition | None
    diagnostics: tuple[object, ...]

    @property
    def valid(self) -> bool:
        return self.definition is not None and not _has_error_diagnostic(self.diagnostics)


@dataclass(frozen=True)
class _CachedWorkflow:
    fingerprint: str
    result: WorkflowLoadResult


_CACHE: dict[Path, _CachedWorkflow] = {}


def load_workflow_definition(path: Path) -> WorkflowLoadResult:
    try:
        workflow_path = path.resolve()
        source = workflow_path.read_bytes()
    except OSError as exc:
        diagnostic_path = str(path)
        return WorkflowLoadResult(
            definition=None,
            diagnostics=(WorkflowCompileDiagnostic(
                code="workflow.load.failed",
                message=f"Cannot read workflow file: {exc}",
                path=diagnostic_path,
            ),),
        )

    fingerprint = hashlib.sha256(source).hexdigest()
    cached = _CACHE.get(workflow_path)
    if cached is not None and cached.fingerprint == fingerprint:
        return cached.result

    try:
        source_text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        result = WorkflowLoadResult(
            definition=None,
            diagnostics=(WorkflowCompileDiagnostic(
                code="workflow.load.failed",
                message=f"Workflow file is not valid UTF-8: {exc}",
                path=str(workflow_path),
            ),),
        )
        _CACHE[workflow_path] = _CachedWorkflow(fingerprint=fingerprint, result=result)
        return result

    parsed = workflow_engine.parse_yaml(source_text, source_name=str(workflow_path))
    if parsed.diagnostics:
        result = WorkflowLoadResult(definition=None, diagnostics=parsed.diagnostics)
        _CACHE[workflow_path] = _CachedWorkflow(fingerprint=fingerprint, result=result)
        return result

    ast = workflow_engine.build_ast(parsed.value, source_name=str(workflow_path))
    if ast.diagnostics or ast.document is None:
        result = WorkflowLoadResult(definition=None, diagnostics=ast.diagnostics)
        _CACHE[workflow_path] = _CachedWorkflow(fingerprint=fingerprint, result=result)
        return result

    validation = workflow_engine.validate(ast.document)
    if not validation.valid:
        result = WorkflowLoadResult(definition=None, diagnostics=validation.diagnostics)
        _CACHE[workflow_path] = _CachedWorkflow(fingerprint=fingerprint, result=result)
        return result

    compiled = compile_workflow(ast.document)
    instruction_diagnostics = (
        _instruction_diagnostics(compiled.definition, workflow_path.parent)
        if compiled.definition is not None
        else ()
    )
    result = WorkflowLoadResult(
        definition=compiled.definition,
        diagnostics=(*compiled.diagnostics, *instruction_diagnostics),
    )
    _CACHE[workflow_path] = _CachedWorkflow(fingerprint=fingerprint, result=result)
    return result


def clear_workflow_cache() -> None:
    _CACHE.clear()


def _has_error_diagnostic(diagnostics: tuple[object, ...]) -> bool:
    return any(getattr(diag, "severity", "error") == "error" for diag in diagnostics)


def _instruction_diagnostics(
    definition: WorkflowDefinition,
    project_root: Path,
) -> tuple[WorkflowCompileDiagnostic, ...]:
    refs = {
        ref
        for owner in (*definition.workers.values(), *definition.task_types.values(), *definition.transitions.values())
        for ref in owner.instructions
    }
    root = project_root / "agents"
    diagnostics: list[WorkflowCompileDiagnostic] = []
    for ref in sorted(refs):
        path = Path(ref)
        if path.is_absolute() or ".." in path.parts:
            diagnostics.append(WorkflowCompileDiagnostic(
                code="instructions.invalid_ref",
                message=f"Instruction reference must stay inside agents/: {ref}",
                path=str(project_root / "workflow.yaml"),
            ))
            continue
        candidates = [root / f"{ref}.agent.md", root / ref] if path.suffix != ".md" else [root / path]
        if sum(candidate.is_file() for candidate in candidates) != 1:
            diagnostics.append(WorkflowCompileDiagnostic(
                code="instructions.not_found",
                message=f"Instruction reference must resolve to exactly one agents/*.agent.md file: {ref}",
                path=str(project_root / "workflow.yaml"),
            ))
    return tuple(diagnostics)
