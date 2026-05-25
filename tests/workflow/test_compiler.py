from __future__ import annotations

from pathlib import Path

import workflow_engine
from open_tulid.workflow import (
    CompileResult,
    WorkflowCompileDiagnostic,
    compile_workflow,
)

FIXTURES = Path(__file__).parent.parent / "workflow_engine" / "fixtures"


def _build_document(yaml_source: str) -> workflow_engine.WorkflowDocument:
    parsed = workflow_engine.parse_yaml(yaml_source)
    assert parsed.value is not None, f"parse failed: {parsed.diagnostics}"
    ast_result = workflow_engine.build_ast(parsed.value)
    assert ast_result.document is not None, f"ast build failed: {ast_result.diagnostics}"
    return ast_result.document


def test_compiles_storage_mapping_into_domain_definition():
    doc = _build_document("""
schema_version: 1
storage:
  boards:
    Work: kanban/Work.md
  state_mappings:
    - state: Todo
      board: Work
      column: Todo
statements:
  - kind: state
    id: Todo
""")

    result = compile_workflow(doc)

    assert result.valid is True
    assert result.definition is not None
    assert result.definition.storage is not None
    assert dict(result.definition.storage.config["boards"]) == {"Work": "kanban/Work.md"}
    assert result.definition.storage.config["state_mappings"][0]["state"] == "Todo"


def test_rejects_storage_mapping_to_unknown_state():
    doc = _build_document("""
schema_version: 1
storage:
  boards:
    Work: kanban/Work.md
  state_mappings:
    - state: Missing
      board: Work
      column: Todo
statements:
  - kind: state
    id: Todo
""")

    result = compile_workflow(doc)

    assert result.valid is False
    assert result.diagnostics[0].code == "workflow.compile.unknown_state_ref"


def test_compiles_scheduler_default_transition_flag():
    doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Review
  - kind: task_type
    id: task
  - kind: worker
    id: codex
  - kind: transition
    id: Implement
    task_type: task
    from: Todo
    to: Review
    worker: codex
    default_for_scheduler: true
""")

    result = compile_workflow(doc)

    assert result.valid is True
    assert result.definition is not None
    assert result.definition.transitions["Implement"].default_for_scheduler is True


def test_compiles_changed_file_requirement_into_domain_definition():
    doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Review
  - kind: task_type
    id: task
  - kind: transition
    id: Implement
    task_type: task
    from: Todo
    to: Review
    requires:
      changed_files:
        required: true
""")

    result = compile_workflow(doc)

    assert result.valid is True
    assert result.definition is not None
    assert result.definition.transitions["Implement"].requires.changed_files_required is True


def test_compiles_task_type_instruction_refs():
    doc = _build_document("""
schema_version: 1
statements:
  - kind: task_type
    id: BackendTask
    instructions: backend-python
""")

    result = compile_workflow(doc)

    assert result.valid is True
    assert result.definition is not None
    assert result.definition.task_types["BackendTask"].instructions == ("backend-python",)


class TestCompilePositive:
    def test_compile_valid_minimal(self):
        result = workflow_engine.load_yaml(str(FIXTURES / "valid_minimal.yaml"))
        assert result.value is not None
        ast_result = workflow_engine.build_ast(result.value)
        assert ast_result.document is not None
        compile_result = compile_workflow(ast_result.document)
        assert compile_result.valid is True
        assert compile_result.definition is not None
        assert compile_result.diagnostics == ()

    def test_compile_valid_full_rejects_custom_types(self):
        result = workflow_engine.load_yaml(str(FIXTURES / "valid_full.yaml"))
        assert result.value is not None
        ast_result = workflow_engine.build_ast(result.value)
        assert ast_result.document is not None
        compile_result = compile_workflow(ast_result.document)
        assert compile_result.valid is False
        assert compile_result.definition is None
        codes = {d.code for d in compile_result.diagnostics}
        assert "workflow.compile.unsupported_validation" in codes
        assert "workflow.compile.unsupported_operation" in codes

    def test_compile_with_builtin_validation(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: file_exists
""")
        result = compile_workflow(doc)
        assert result.valid is True
        assert result.definition is not None
        assert "file_exists" in result.definition.validation_types

    def test_compile_with_builtin_operation(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: operation_type
    id: move_task
""")
        result = compile_workflow(doc)
        assert result.valid is True
        assert result.definition is not None
        assert "move_task" in result.definition.operation_types

    def test_compile_with_builtin_worker_type(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: worker
    id: my_worker
    type: local_llm
""")
        result = compile_workflow(doc)
        assert result.valid is True
        assert result.definition is not None
        assert "my_worker" in result.definition.workers
        worker = result.definition.workers["my_worker"]
        assert worker.implementation_id == "local_llm"

    def test_compile_with_worker_no_type(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: worker
    id: noop
""")
        result = compile_workflow(doc)
        assert result.valid is True
        assert result.definition is not None
        worker = result.definition.workers["noop"]
        assert worker.implementation_id == "noop"

    def test_produces_immutable_definition(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(Exception):
            result.definition.schema_version = 999

    def test_preserves_states(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        assert "Todo" in result.definition.states
        assert "Done" in result.definition.states

    def test_preserves_task_types(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: CodingTask
    requirements:
      Todo:
        artifacts: []
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        assert "CodingTask" in result.definition.task_types

    def test_preserves_artifact_types(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: artifact_type
    id: Summary
    template: templates/summary.md
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        assert "Summary" in result.definition.artifact_types
        art = result.definition.artifact_types["Summary"]
        assert art.template == "templates/summary.md"

    def test_preserves_validation_types(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: file_exists
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        assert "file_exists" in result.definition.validation_types
        val = result.definition.validation_types["file_exists"]
        assert val.implementation_id == "file_exists"

    def test_preserves_operation_types(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: operation_type
    id: move_task
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        assert "move_task" in result.definition.operation_types
        op = result.definition.operation_types["move_task"]
        assert op.implementation_id == "move_task"

    def test_preserves_workers(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: worker
    id: my_worker
    type: local_llm
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        assert "my_worker" in result.definition.workers

    def test_preserves_transitions(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
  - kind: task_type
    id: TT
  - kind: worker
    id: w
    type: noop
  - kind: transition
    id: moveToDone
    task_type: TT
    from: Todo
    to: Done
    worker: w
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        assert "moveToDone" in result.definition.transitions
        trans = result.definition.transitions["moveToDone"]
        assert trans.task_type == "TT"
        assert trans.from_state == "Todo"
        assert trans.to_state == "Done"
        assert trans.worker == "w"

    def test_compile_result_valid_true(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        result = compile_workflow(doc)
        assert result.valid is True

    def test_compile_result_is_compile_result(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        result = compile_workflow(doc)
        assert isinstance(result, CompileResult)

    def test_compiles_to_domain_workflow_definition(self):
        from open_tulid.domain.schema import WorkflowDefinition

        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        result = compile_workflow(doc)
        assert isinstance(result.definition, WorkflowDefinition)

    def test_workflow_definition_api_reexports_domain_model(self):
        from open_tulid.domain.schema import WorkflowDefinition as DomainWorkflowDefinition
        from open_tulid.workflow import WorkflowDefinition as WorkflowApiWorkflowDefinition

        assert WorkflowApiWorkflowDefinition is DomainWorkflowDefinition

    def test_default_registries_used(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: file_exists
""")
        result = compile_workflow(doc)
        assert result.valid is True


class TestCompileNegative:
    def test_unsupported_validation(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: imaginary_validator
""")
        result = compile_workflow(doc)
        assert result.valid is False
        assert result.definition is None
        assert any(d.code == "workflow.compile.unsupported_validation" for d in result.diagnostics)

    def test_unsupported_operation(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: operation_type
    id: imaginary_operation
""")
        result = compile_workflow(doc)
        assert result.valid is False
        assert result.definition is None
        assert any(d.code == "workflow.compile.unsupported_operation" for d in result.diagnostics)

    def test_unsupported_worker_type(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: worker
    id: my_worker
    type: imaginary_worker
""")
        result = compile_workflow(doc)
        assert result.valid is False
        assert result.definition is None
        assert any(d.code == "workflow.compile.unsupported_worker" for d in result.diagnostics)

    def test_registry_validation_diagnostics_prevent_compilation(self):
        from open_tulid.workflow import RuntimeRegistries, ValidationSpec as VS
        bad_regs = RuntimeRegistries(
            validations={"v1": VS(id="v1", implementation=None)},
            operations={},
            workers={},
        )
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        result = compile_workflow(doc, registries=bad_regs)
        assert result.valid is False
        assert result.definition is None

    def test_diagnostics_preserve_span(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: imaginary_validator
""")
        result = compile_workflow(doc)
        for d in result.diagnostics:
            if d.code == "workflow.compile.unsupported_validation":
                assert d.path is not None

    def test_no_exception_on_compile_error(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: imaginary_validator
  - kind: operation_type
    id: imaginary_operation
  - kind: worker
    id: w
    type: imaginary_worker
""")
        result = compile_workflow(doc)
        assert result.valid is False
        assert result.definition is None
        codes = {d.code for d in result.diagnostics}
        assert "workflow.compile.unsupported_validation" in codes
        assert "workflow.compile.unsupported_operation" in codes
        assert "workflow.compile.unsupported_worker" in codes

    def test_collects_all_diagnostics(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: fake1
  - kind: validation_type
    id: fake2
""")
        result = compile_workflow(doc)
        assert result.valid is False
        unsupported = [d for d in result.diagnostics if d.code == "workflow.compile.unsupported_validation"]
        assert len(unsupported) == 2

    def test_diagnostics_are_workflow_compile_diagnostics(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: imaginary_validator
""")
        result = compile_workflow(doc)
        for d in result.diagnostics:
            assert isinstance(d, WorkflowCompileDiagnostic)

    def test_valid_property_false_on_errors(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: imaginary_validator
""")
        result = compile_workflow(doc)
        assert result.valid is False

    def test_valid_property_true_on_success(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        result = compile_workflow(doc)
        assert result.valid is True


class TestArtifactTypeTemplates:
    def test_artifact_template_reference_compiles(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: artifact_type
    id: Summary
    template: templates/summary.md
""")
        result = compile_workflow(doc)
        assert result.valid is True
        assert result.definition is not None
        art = result.definition.artifact_types["Summary"]
        assert art.template == "templates/summary.md"

    def test_artifact_template_string_compiles(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: artifact_type
    id: Report
    template: templates/report-v2.md
""")
        result = compile_workflow(doc)
        assert result.valid is True
        assert result.definition is not None
        art = result.definition.artifact_types["Report"]
        assert art.template == "templates/report-v2.md"

    def test_artifact_without_template_compiles(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: artifact_type
    id: RawData
""")
        result = compile_workflow(doc)
        assert result.valid is True
        assert result.definition is not None
        art = result.definition.artifact_types["RawData"]
        assert art.template is None


class TestDeepImmutability:
    def test_top_level_states_map_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.states["Hacked"] = type('StateDefinition', (), {'id': 'Hacked'})()

    def test_top_level_task_types_map_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.task_types["Hacked"] = type('TaskTypeDefinition', (), {'id': 'Hacked', 'requirements_by_state': {}})()

    def test_top_level_artifact_types_map_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: artifact_type
    id: A
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.artifact_types["Hacked"] = type('ArtifactTypeDefinition', (), {'id': 'Hacked'})()

    def test_top_level_validation_types_map_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: file_exists
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.validation_types["Hacked"] = object()

    def test_top_level_operation_types_map_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: operation_type
    id: move_task
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.operation_types["Hacked"] = object()

    def test_top_level_workers_map_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: worker
    id: w
    type: noop
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.workers["Hacked"] = object()

    def test_top_level_transitions_map_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
  - kind: task_type
    id: TT
  - kind: worker
    id: w
    type: noop
  - kind: transition
    id: t1
    task_type: TT
    from: Todo
    to: Done
    worker: w
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.transitions["Hacked"] = object()

    def test_nested_requirements_by_state_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
    requirements:
      Todo:
        artifacts: []
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.task_types["TT"].requirements_by_state["Hacked"] = object()

    def test_nested_validation_args_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: file_exists
    args:
      path:
        type: string
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.validation_types["file_exists"].args["hacked"] = object()

    def test_nested_operation_args_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: operation_type
    id: move_task
    args:
      target:
        type: string
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        with pytest.raises(TypeError):
            result.definition.operation_types["move_task"].args["hacked"] = object()

    def test_validation_call_args_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
  - kind: task_type
    id: TT
  - kind: worker
    id: w
    type: noop
  - kind: validation_type
    id: file_exists
    args:
      path:
        type: string
  - kind: transition
    id: t1
    task_type: TT
    from: Todo
    to: Done
    worker: w
    requires:
      validations:
        - type: file_exists
          args:
            path: /tmp/x
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        vc = result.definition.transitions["t1"].requires.validations[0]
        with pytest.raises(TypeError):
            vc.args["hacked"] = object()

    def test_validation_call_nested_args_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
  - kind: artifact_type
    id: ImplementationSummary
  - kind: validation_type
    id: file_exists
    args:
      artifacts:
        type: artifact_ref
        many: true
  - kind: transition
    id: t1
    task_type: TT
    from: Todo
    to: Todo
    requires:
      validations:
        - type: file_exists
          args:
            artifacts:
              - ImplementationSummary
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        vc = result.definition.transitions["t1"].requires.validations[0]
        assert vc.args["artifacts"] == ("ImplementationSummary",)
        with pytest.raises(TypeError):
            vc.args["artifacts"][0] = "Other"

    def test_operation_call_args_immutable(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
  - kind: task_type
    id: TT
  - kind: worker
    id: w
    type: noop
  - kind: operation_type
    id: move_task
    args:
      target:
        type: string
  - kind: transition
    id: t1
    task_type: TT
    from: Todo
    to: Done
    worker: w
    transaction:
      steps:
        - op: move_task
          args:
            target: Done
""")
        result = compile_workflow(doc)
        assert result.definition is not None
        import pytest
        step = result.definition.transitions["t1"].transaction.steps[0]
        with pytest.raises(TypeError):
            step.args["hacked"] = object()


class TestCrossRefSpans:
    def test_unknown_transition_operation_includes_span(self):
        result = workflow_engine.load_yaml(str(FIXTURES / "valid_full.yaml"))
        assert result.value is not None
        ast_result = workflow_engine.build_ast(result.value)
        assert ast_result.document is not None
        compile_result = compile_workflow(ast_result.document)
        for d in compile_result.diagnostics:
            if d.code == "workflow.compile.unknown_operation_ref":
                assert d.path is not None
                assert d.line is not None
                assert d.column is not None

    def test_unknown_transition_worker_includes_span(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
  - kind: task_type
    id: TT
  - kind: transition
    id: t1
    task_type: TT
    from: Todo
    to: Done
    worker: nonexistent_worker
""")
        result = compile_workflow(doc)
        assert result.valid is False
        for d in result.diagnostics:
            if d.code == "workflow.compile.unknown_worker_ref":
                assert d.path is not None

    def test_unknown_requirement_artifact_includes_span(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: TT
    requirements:
      Todo:
        artifacts:
          - NonexistentArtifact
""")
        result = compile_workflow(doc)
        assert result.valid is False
        for d in result.diagnostics:
            if d.code == "workflow.compile.unknown_artifact_ref":
                assert d.path is not None

    def test_unknown_state_in_task_type_includes_span(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: task_type
    id: TT
    requirements:
      NonexistentState:
        artifacts: []
""")
        result = compile_workflow(doc)
        assert result.valid is False
        for d in result.diagnostics:
            if d.code == "workflow.compile.unknown_state_ref":
                assert d.path is not None

    def test_unknown_task_type_in_transition_includes_span(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: state
    id: Done
  - kind: worker
    id: w
    type: noop
  - kind: transition
    id: t1
    task_type: NonexistentTT
    from: Todo
    to: Done
    worker: w
""")
        result = compile_workflow(doc)
        assert result.valid is False
        for d in result.diagnostics:
            if d.code == "workflow.compile.unknown_task_type_ref":
                assert d.path is not None

    def test_unknown_from_state_in_transition_includes_span(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Done
  - kind: task_type
    id: TT
  - kind: worker
    id: w
    type: noop
  - kind: transition
    id: t1
    task_type: TT
    from: NonexistentFrom
    to: Done
    worker: w
""")
        result = compile_workflow(doc)
        assert result.valid is False
        for d in result.diagnostics:
            if d.code == "workflow.compile.unknown_state_ref":
                assert d.path is not None


class TestDiagnosticCodes:
    def test_unsupported_validation_code_exists(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: validation_type
    id: nonexistent
""")
        result = compile_workflow(doc)
        codes = {d.code for d in result.diagnostics}
        assert "workflow.compile.unsupported_validation" in codes

    def test_unsupported_operation_code_exists(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: operation_type
    id: nonexistent
""")
        result = compile_workflow(doc)
        codes = {d.code for d in result.diagnostics}
        assert "workflow.compile.unsupported_operation" in codes

    def test_unsupported_worker_code_exists(self):
        doc = _build_document("""
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: worker
    id: w
    type: nonexistent
""")
        result = compile_workflow(doc)
        codes = {d.code for d in result.diagnostics}
        assert "workflow.compile.unsupported_worker" in codes
