# Workflow Engine DSL Implementation Contract

## Audience

This spec is written for a local coding model implementing the first standalone `workflow_engine` module.

Treat this as an implementation contract, not a design essay. Follow the required names, API shapes, validation rules, and tests exactly unless the existing repository structure forces a harmless path adjustment.

## Goal

Implement a standalone Python package named `workflow_engine`.

The package is the frontend for the open-tulid Workflow Engine DSL:

```text
YAML source
-> parsed YAML with source locations
-> immutable AST
-> JSON Schema for editor/shape validation
-> semantic validation through visitors
-> placeholder interpreter visitor for later runtime execution
```

The concrete syntax is YAML. Do not write a parser grammar. Do not use ANTLR, Lark, or a custom tokenizer/parser.

The first implementation validates the DSL. It does not execute the workflow.

## Hard Boundaries

The `workflow_engine` module must be standalone.

Allowed:

- Python standard library
- `ruamel.yaml`
- test dependencies such as `pytest` and `jsonschema`

Forbidden:

- importing `open_tulid.domain`
- importing `open_tulid.cli`
- importing `open_tulid.vault`
- moving tasks
- writing task files
- writing event logs
- writing transaction journals
- running workers
- running validations
- calling git
- calling network endpoints

The module may live inside the same repository, but it must not depend on the existing application code.

## Required Package Layout

Use this layout unless the repository strongly suggests a small variation:

```text
src/workflow_engine/
  __init__.py
  ast.py
  diagnostics.py
  langdef.py
  loader.py
  schema.py
  symbols.py
  validation.py
  visitors.py

tests/workflow_engine/
  fixtures/
  test_loader.py
  test_validation.py
  test_schema.py
  test_visitors.py
```

`langdef.py` is required. It is the single source of truth for:

- supported statement kinds
- allowed keys per statement kind
- supported argument types
- reference-type-to-symbol-table mapping

Do not duplicate those constants in `loader.py`, `schema.py`, and `validation.py`.

## Concrete YAML Shape

The first version supports exactly one workflow document per file.

Required top-level shape:

```yaml
schema_version: 1
statements:
  - kind: state
    id: Todo
```

Rules:

- Root must be a mapping.
- `schema_version` is required.
- `schema_version` must be integer `1`; boolean is not an integer.
- `statements` is required.
- `statements` must be a list.
- The statements list may be empty only if tests explicitly allow it. Prefer allowing an empty list for AST tests, but real fixtures should include statements.
- Unknown top-level keys are errors.
- Every statement must be a mapping.
- Every statement must have `kind`.
- `kind` must be a string.
- `kind` must be one of the supported kinds.
- Every statement must have `id`.
- `id` must be a string.
- Unknown keys inside statements are errors.
- Comments are accepted and discarded.
- Statement order must not affect validation. Forward references are allowed.

Supported statement kinds:

- `state`
- `task_type`
- `artifact_type`
- `validation_type`
- `worker`
- `operation_type`
- `transition`

Do not add `state_type`.
Do not add error workflow statements.
Do not add project statements.
Do not add multi-file composition yet.

## Public API

The public package API must expose these functions and result types from `workflow_engine.__init__`.

```python
@dataclass(frozen=True)
class ParseResult:
    value: object | None
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class AstBuildResult:
    document: WorkflowDocument | None
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    diagnostics: tuple[Diagnostic, ...]

def parse_yaml(source: str, *, source_name: str = "<memory>") -> ParseResult: ...

def load_yaml(path: str | Path) -> ParseResult: ...

def build_ast(parsed: object | None, *, source_name: str = "<memory>") -> AstBuildResult: ...

def validate(document: WorkflowDocument) -> ValidationResult: ...

def generate_json_schema() -> dict: ...

def write_json_schema(path: str | Path) -> None: ...
```

Do not expose only tuple-return APIs. Internally you may use helper tuples, but the public API must return the result dataclasses above.

Normal DSL/user errors must never raise exceptions. They must return diagnostics.

Examples of normal user errors:

- malformed YAML
- root is a list
- missing `schema_version`
- unknown statement kind
- unknown key
- validation call is not a mapping
- transaction step is not a mapping
- missing required transition field
- unknown state reference
- wrong argument type

Exceptions are acceptable only for programmer mistakes or impossible internal states.

## Diagnostics

Use this exact diagnostic model:

```python
@dataclass(frozen=True)
class SourceSpan:
    path: str
    line: int | None = None
    column: int | None = None

@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    path: str | None = None
    line: int | None = None
    column: int | None = None
```

Line and column numbers must be one-based.

Diagnostic requirements:

- `code` must be stable and asserted by tests.
- `message` must be human-readable.
- `severity` is `"error"` or `"warning"`.
- `path` is a YAML path such as `statements[5].to`.
- `line` and `column` must be populated whenever `ruamel.yaml` can provide them.
- Parse errors must use the provided `source_name` as path when no better path exists.
- Shape errors should point at the offending field or list item, not just the parent statement.
- Semantic errors should point at the offending reference field or argument when possible.

Do not emit placeholder paths like:

```text
statements[?].to
statements[0].to
```

unless the real offending statement is actually statement index `0`.

Required diagnostic codes:

```text
workflow.yaml.parse_error
workflow.shape.root_not_mapping
workflow.shape.missing_required_field
workflow.shape.wrong_type
workflow.shape.unknown_key
workflow.schema.unsupported_version
workflow.statement.unknown_kind
workflow.symbol.duplicate_id
workflow.reference.unknown_state
workflow.reference.unknown_task_type
workflow.reference.unknown_artifact
workflow.reference.unknown_validation
workflow.reference.unknown_worker
workflow.reference.unknown_operation
workflow.call.missing_required_argument
workflow.call.unknown_argument
workflow.call.wrong_argument_type
workflow.call.unknown_argument_type
```

## Source Locations

Use `ruamel.yaml`.

Do not merely parse with `ruamel.yaml`; actually carry location data into AST spans and diagnostics.

Every AST node must have a `span` when practical:

- `WorkflowDocument`
- every statement
- `ArgSpec`
- `RequirementSet`
- `ValidationCall`
- `OperationCall`
- `TransactionPlan`

Use one-based line/column everywhere. `ruamel.yaml` stores zero-based locations, so convert exactly once at the boundary.

Recommended helper behavior:

```python
def node_span(node, path: str) -> SourceSpan: ...
def key_span(mapping, key, path: str) -> SourceSpan: ...
def list_item_span(sequence, index, path: str) -> SourceSpan: ...
```

For a semantic diagnostic like unknown `to` state, use the span/path of the actual `to` field if available:

```text
path = statements[7].to
line = line where the `to` key/value occurs
column = column where the `to` key/value occurs
```

## AST Requirements

Use immutable dataclasses: `@dataclass(frozen=True)`.

All AST objects are immutable. If a future pass needs to transform the AST, it must create a new AST.

Required AST model:

```python
@dataclass(frozen=True)
class WorkflowDocument:
    schema_version: int
    statements: tuple[Statement, ...]
    span: SourceSpan | None = None

@dataclass(frozen=True)
class Statement:
    id: str
    span: SourceSpan | None = None
    def accept(self, visitor: "AstVisitor") -> object: ...
```

Concrete statements must inherit from `Statement`:

```python
@dataclass(frozen=True)
class StateStatement(Statement): ...

@dataclass(frozen=True)
class TaskTypeStatement(Statement):
    requirements_by_state: Mapping[str, RequirementSet] = field(default_factory=dict)

@dataclass(frozen=True)
class ArtifactTypeStatement(Statement):
    template: str | None = None

@dataclass(frozen=True)
class ValidationTypeStatement(Statement):
    args: Mapping[str, ArgSpec] = field(default_factory=dict)

@dataclass(frozen=True)
class WorkerStatement(Statement):
    type: str | None = None

@dataclass(frozen=True)
class OperationTypeStatement(Statement):
    args: Mapping[str, ArgSpec] = field(default_factory=dict)

@dataclass(frozen=True)
class TransitionStatement(Statement):
    task_type: str
    from_state: str
    to_state: str
    worker: str | None = None
    requires: RequirementSet = field(default_factory=RequirementSet)
    transaction: TransactionPlan | None = None
```

Nested AST nodes:

```python
@dataclass(frozen=True)
class ArgSpec:
    type: str
    required: bool = False
    many: bool = False
    span: SourceSpan | None = None

@dataclass(frozen=True)
class ValidationCall:
    type: str
    args: Mapping[str, object] = field(default_factory=dict)
    span: SourceSpan | None = None

@dataclass(frozen=True)
class RequirementSet:
    artifacts: tuple[str, ...] = ()
    validations: tuple[ValidationCall, ...] = ()
    span: SourceSpan | None = None

@dataclass(frozen=True)
class OperationCall:
    op: str
    args: Mapping[str, object] = field(default_factory=dict)
    span: SourceSpan | None = None

@dataclass(frozen=True)
class TransactionPlan:
    steps: tuple[OperationCall, ...]
    span: SourceSpan | None = None
```

Every AST node, including nested nodes, must implement `accept(visitor)`.

## Visitor Architecture

This project intentionally uses a visitor pattern.

Required visitor protocol:

```python
class AstVisitor(Protocol):
    def visit_document(self, node: WorkflowDocument) -> object: ...
    def visit_state(self, node: StateStatement) -> object: ...
    def visit_task_type(self, node: TaskTypeStatement) -> object: ...
    def visit_artifact_type(self, node: ArtifactTypeStatement) -> object: ...
    def visit_validation_type(self, node: ValidationTypeStatement) -> object: ...
    def visit_worker(self, node: WorkerStatement) -> object: ...
    def visit_operation_type(self, node: OperationTypeStatement) -> object: ...
    def visit_transition(self, node: TransitionStatement) -> object: ...
    def visit_arg_spec(self, node: ArgSpec) -> object: ...
    def visit_validation_call(self, node: ValidationCall) -> object: ...
    def visit_requirement_set(self, node: RequirementSet) -> object: ...
    def visit_operation_call(self, node: OperationCall) -> object: ...
    def visit_transaction_plan(self, node: TransactionPlan) -> object: ...
```

Required concrete visitors:

- `ValidationVisitor`
- `InterpretationVisitor`

`validate(document)` must use `ValidationVisitor`. Do not create a second unrelated semantic validator that bypasses the exported visitor.

Nested validation should be visitor-driven. Helper methods are allowed, but traversal should happen through `accept()` on nested nodes wherever possible.

`InterpretationVisitor` is a placeholder only. It must not execute workflow behavior. It may raise `NotImplementedError` for visit methods or return placeholder data, but it must not run workers, operations, or validations.

## Statement Definitions

### `state`

YAML:

```yaml
- kind: state
  id: Todo
```

Allowed keys:

- `kind`
- `id`

### `task_type`

A task type defines requirements by state. A task plus state results in a list of requirements.

YAML:

```yaml
- kind: task_type
  id: CodingTask
  requirements:
    CodeReview:
      artifacts:
        - ImplementationSummary
      validations:
        - type: exists
          args:
            artifact: ImplementationSummary
```

Allowed keys:

- `kind`
- `id`
- `requirements`

Rules:

- `requirements` is optional.
- If present, `requirements` must be a mapping.
- Requirement keys must be state ID strings.
- Requirement keys must resolve to declared `state` statements during semantic validation.

### `artifact_type`

YAML:

```yaml
- kind: artifact_type
  id: ImplementationSummary
  template: templates/implementation-summary.md
```

Allowed keys:

- `kind`
- `id`
- `template`

Rules:

- `template` is optional.
- If present, `template` must be a string.
- Do not check template file existence in this module.

### `validation_type`

Validation types declare possible validations. They do not execute.

YAML:

```yaml
- kind: validation_type
  id: exists
  args:
    artifact:
      type: artifact_ref
      required: true
```

Allowed keys:

- `kind`
- `id`
- `args`

Rules:

- `args` is optional.
- If present, `args` must be a mapping of argument name to `ArgSpec`.
- Every arg spec must have `type`.
- `required` defaults to `false`.
- `many` defaults to `false`.

### `worker`

Workers are declarations only. They do not execute.

YAML:

```yaml
- kind: worker
  id: qwen_local
  type: local_llm
```

Allowed keys:

- `kind`
- `id`
- `type`

Rules:

- `type` is optional generic metadata.
- If present, `type` must be a string.

### `operation_type`

Operation types declare allowed transaction operations. They do not execute.

YAML:

```yaml
- kind: operation_type
  id: move_task
  args:
    to:
      type: state_ref
      required: true
```

Allowed keys:

- `kind`
- `id`
- `args`

Rules are the same as `validation_type.args`.

### `transition`

Transitions connect task type, source state, target state, optional worker, requirements, and transaction plan.

YAML:

```yaml
- kind: transition
  id: ImplementTask
  task_type: CodingTask
  from: Todo
  to: CodeReview
  worker: qwen_local
  requires:
    artifacts:
      - ImplementationSummary
    validations:
      - type: exists
        args:
          artifact: ImplementationSummary
  transaction:
    steps:
      - op: validate_requirements
        args:
          state: CodeReview
      - op: move_task
        args:
          to: CodeReview
```

Allowed keys:

- `kind`
- `id`
- `task_type`
- `from`
- `to`
- `worker`
- `requires`
- `transaction`

Required keys:

- `kind`
- `id`
- `task_type`
- `from`
- `to`

Rules:

- If any required transition field is missing, do not build that `TransitionStatement`.
- `task_type` must be a string.
- `from` must be a string.
- `to` must be a string.
- `worker`, if present, must be a string.
- `requires` is optional.
- `transaction` is optional.
- `transaction.steps`, if `transaction` exists, is required and must be a list.

Semantic rule:

To move into the transition target state, later runtime will consider:

```text
task_type + target_state requirements
+ transition.requires
```

This module only validates declaration and references. It does not determine fulfillment.

## RequirementSet Shape

YAML:

```yaml
artifacts:
  - ImplementationSummary
validations:
  - type: exists
    args:
      artifact: ImplementationSummary
```

Rules:

- Requirement set must be a mapping.
- Allowed keys: `artifacts`, `validations`.
- `artifacts` is optional.
- If present, `artifacts` must be a list of strings.
- Each artifact string must resolve to an `artifact_type` during semantic validation.
- `validations` is optional.
- If present, `validations` must be a list of `ValidationCall` mappings.

## ValidationCall Shape

YAML:

```yaml
- type: exists
  args:
    artifact: ImplementationSummary
```

Rules:

- Validation call must be a mapping.
- Allowed keys: `type`, `args`.
- `type` is required.
- `type` must be a string.
- `type` must resolve to a `validation_type` during semantic validation.
- `args` is optional.
- If present, `args` must be a mapping.
- Unknown args are errors.
- Missing required args are errors.
- Arg values must match declared arg specs.

## TransactionPlan And OperationCall Shape

YAML:

```yaml
transaction:
  steps:
    - op: move_task
      args:
        to: CodeReview
```

Rules:

- Transaction plan must be a mapping.
- Allowed transaction keys: `steps`.
- `steps` is required.
- `steps` must be a list.
- Each step must be an `OperationCall` mapping.
- Operation call allowed keys: `op`, `args`.
- `op` is required.
- `op` must be a string.
- `op` must resolve to an `operation_type` during semantic validation.
- `args` is optional.
- If present, `args` must be a mapping.
- Unknown args are errors.
- Missing required args are errors.
- Arg values must match declared arg specs.

## ArgSpec And Arg Value Validation

Supported arg spec types:

- `string`
- `integer`
- `boolean`
- `state_ref`
- `task_type_ref`
- `artifact_ref`
- `validation_ref`
- `worker_ref`
- `operation_ref`

Arg spec shape:

```yaml
arg_name:
  type: artifact_ref
  required: true
  many: false
```

Rules:

- Arg spec must be a mapping.
- Allowed keys: `type`, `required`, `many`.
- `type` is required.
- Unknown arg spec keys are errors.
- Unknown arg spec type is an error.
- `required`, if present, must be boolean.
- `many`, if present, must be boolean.
- If `many: true`, the supplied call arg value must be a list.
- If `many: false`, the supplied call arg value must be a scalar.

Scalar type validation:

```text
string -> value must be str
integer -> value must be int but not bool
boolean -> value must be bool
```

Reference type validation:

```text
state_ref -> value must be str and resolve to state
task_type_ref -> value must be str and resolve to task_type
artifact_ref -> value must be str and resolve to artifact_type
validation_ref -> value must be str and resolve to validation_type
worker_ref -> value must be str and resolve to worker
operation_ref -> value must be str and resolve to operation_type
```

For `many: true`, validate every list item against the declared type and reference table.

This is mandatory. Do not only check that the argument name exists.

## Symbol Tables

Build symbol tables after AST construction and before semantic validation.

IDs are unique per statement kind.

Allowed:

```yaml
- kind: state
  id: Todo
- kind: task_type
  id: Todo
```

Rejected:

```yaml
- kind: state
  id: Todo
- kind: state
  id: Todo
```

Symbol tables must include:

- states
- task types
- artifact types
- validation types
- workers
- operation types
- transitions

Semantic validation must use symbol tables for references. Do not scan the statement tuple repeatedly to find referenced types if a symbol table exists.

## Build AST Rules

`build_ast` has two jobs:

1. Shape validation.
2. Immutable AST construction.

Important rule:

If a statement has shape errors that make a valid node impossible, do not construct that statement.

Examples:

- transition missing `task_type` -> emit diagnostic, do not construct the transition node
- transition missing `from` -> emit diagnostic, do not construct the transition node
- validation call missing `type` -> emit diagnostic, do not construct that validation call
- operation call missing `op` -> emit diagnostic, do not construct that operation call
- arg spec has unknown type -> emit diagnostic, do not construct that arg spec

If any statement-level shape diagnostics exist, `AstBuildResult.document` should be `None`.

This avoids partially valid ASTs with placeholder values like:

```python
TransitionStatement(task_type="", from_state="", to_state="")
```

Do not use empty string placeholders for required fields.

## Validation Rules

`validate(document)` must:

1. Build symbol tables.
2. Detect duplicate IDs by kind.
3. Visit the AST with `ValidationVisitor`.
4. Validate references.
5. Validate call args against declared arg specs.
6. Collect all diagnostics it can.

Reference validation must check:

- task type requirement state IDs
- task type requirement artifact IDs
- task type requirement validation calls
- transition `task_type`
- transition `from`
- transition `to`
- transition `worker`
- transition required artifact IDs
- transition validation calls
- transition operation calls
- validation call args that use reference types
- operation call args that use reference types

Call arg validation must check:

- required arg is present
- no unknown arg names
- scalar values match type
- `many` list/scalar rules
- reference values resolve

## JSON Schema Generation

`generate_json_schema()` must produce a JSON Schema dict.

The schema is for editor/shape validation. It does not replace semantic validation.

The schema must be generated from shared language-definition data in `langdef.py`. Do not manually duplicate statement definitions only inside `schema.py`.

`langdef.py` should contain the statement kind table and arg type table. `schema.py`, `loader.py`, and `validation.py` should import those definitions.

The schema must validate:

- top-level object shape
- `schema_version`
- `statements`
- supported statement kinds
- required keys by kind
- allowed keys by kind
- arg spec shape
- requirement set shape
- validation call shape
- transaction plan shape
- operation call shape

The schema does not need to validate cross-statement references.

## Example Valid Workflow

Use this as a required fixture named `valid_full.yaml`.

```yaml
schema_version: 1
statements:
  - kind: state
    id: Todo

  - kind: state
    id: CodeReview

  - kind: task_type
    id: CodingTask
    requirements:
      CodeReview:
        artifacts:
          - ImplementationSummary
          - TestResult
        validations:
          - type: exists
            args:
              artifact: ImplementationSummary

  - kind: artifact_type
    id: ImplementationSummary
    template: templates/implementation-summary.md

  - kind: artifact_type
    id: TestResult
    template: templates/test-result.md

  - kind: validation_type
    id: exists
    args:
      artifact:
        type: artifact_ref
        required: true

  - kind: validation_type
    id: link_exists
    args:
      artifact:
        type: artifact_ref
        required: true

  - kind: worker
    id: qwen_local
    type: local_llm

  - kind: operation_type
    id: validate_requirements
    args:
      state:
        type: state_ref
        required: true

  - kind: operation_type
    id: promote_artifacts
    args:
      artifacts:
        type: artifact_ref
        many: true

  - kind: operation_type
    id: move_task
    args:
      to:
        type: state_ref
        required: true

  - kind: transition
    id: ImplementTask
    task_type: CodingTask
    from: Todo
    to: CodeReview
    worker: qwen_local
    requires:
      artifacts:
        - ImplementationSummary
        - TestResult
      validations:
        - type: exists
          args:
            artifact: ImplementationSummary
        - type: link_exists
          args:
            artifact: TestResult
    transaction:
      steps:
        - op: validate_requirements
          args:
            state: CodeReview
        - op: promote_artifacts
          args:
            artifacts:
              - ImplementationSummary
              - TestResult
        - op: move_task
          args:
            to: CodeReview
```

## Required Tests

Use fixture files for most tests. Inline tests are fine for small probes, but fixtures are required for realistic examples.

### Positive Tests

- valid minimal document
- valid full document
- comments accepted and discarded
- forward references accepted
- reversed statement order accepted
- AST nodes are immutable
- public APIs return `ParseResult`, `AstBuildResult`, `ValidationResult`
- JSON Schema accepts valid minimal fixture
- JSON Schema accepts valid full fixture
- `InterpretationVisitor` does not execute real behavior

### Shape Negative Tests

- malformed YAML
- root is not a mapping
- missing `schema_version`
- `schema_version` wrong type
- unsupported `schema_version`
- missing `statements`
- `statements` not list
- statement not mapping
- missing `kind`
- non-string `kind`
- unknown `kind`
- missing `id`
- non-string `id`
- unknown top-level key
- unknown statement key
- artifact item not string
- validation call not mapping
- validation call missing `type`
- validation call unknown key
- validation call `args` not mapping
- operation step not mapping
- operation call missing `op`
- operation call unknown key
- operation call `args` not mapping
- arg spec not mapping
- arg spec missing `type`
- arg spec unknown `type`
- arg spec `required` not boolean
- arg spec `many` not boolean
- transition missing `task_type`
- transition missing `from`
- transition missing `to`

### Semantic Negative Tests

- duplicate state ID
- duplicate task type ID
- transition unknown `from` state
- transition unknown `to` state
- transition unknown task type
- transition unknown worker
- task type requirements unknown state
- task type requirements unknown artifact
- task type requirements unknown validation
- transition requirements unknown artifact
- transition requirements unknown validation
- transaction unknown operation
- missing required validation arg
- missing required operation arg
- unknown validation arg
- unknown operation arg
- scalar arg wrong type
- `many: true` arg receives scalar
- `many: false` arg receives list
- `artifact_ref` arg points to unknown artifact
- `state_ref` arg points to unknown state
- `operation_ref` arg points to unknown operation

### Diagnostic Tests

- every diagnostic has code, message, severity
- shape diagnostic has path
- semantic diagnostic has path
- statement-level shape diagnostic has one-based line/column
- nested shape diagnostic has one-based line/column where ruamel can provide it
- semantic reference diagnostic has one-based line/column where span is available
- no diagnostic path uses `statements[?]`
- a transition at statement index 3 must not report path `statements[0].to`

### No-Crash Probe Tests

Add tests for these exact snippets:

```yaml
schema_version: 1
statements:
  - kind: task_type
    id: T
    requirements:
      Todo:
        validations:
          - not-a-map
  - kind: state
    id: Todo
```

Expected: no exception; `AstBuildResult.document is None`; diagnostic `workflow.shape.wrong_type`.

```yaml
schema_version: 1
statements:
  - kind: transition
    id: T
    task_type: TT
    from: Todo
    to: Todo
    transaction:
      steps:
        - not-a-map
```

Expected: no exception; `AstBuildResult.document is None`; diagnostic `workflow.shape.wrong_type`.

```yaml
schema_version: 1
statements:
  - kind: operation_type
    id: bad
    args:
      x:
        type: string
        many: nope
```

Expected: no exception; `AstBuildResult.document is None`; diagnostic `workflow.shape.wrong_type`.

```yaml
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: transition
    id: T
    from: Todo
    to: Done
```

Expected: no exception; `AstBuildResult.document is None`; diagnostic `workflow.shape.missing_required_field`; no partial `TransitionStatement`.

```yaml
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: CodingTask
  - kind: validation_type
    id: exists
    args:
      artifact:
        type: artifact_ref
        required: true
  - kind: transition
    id: T
    task_type: CodingTask
    from: Todo
    to: Todo
    requires:
      validations:
        - type: exists
          args:
            artifact: MissingArtifact
```

Expected: validation fails with `workflow.reference.unknown_artifact`.

```yaml
schema_version: 1
statements:
  - kind: state
    id: Todo
  - kind: task_type
    id: CodingTask
  - kind: validation_type
    id: check
    args:
      count:
        type: integer
        required: true
  - kind: transition
    id: T
    task_type: CodingTask
    from: Todo
    to: Todo
    requires:
      validations:
        - type: check
          args:
            count: not-an-int
```

Expected: validation fails with `workflow.call.wrong_argument_type`.

## Common Mistakes To Avoid

These mistakes happened in previous attempts. Do not repeat them.

1. Do not construct partial AST nodes after required-field errors.
2. Do not use empty strings as placeholders for missing required fields.
3. Do not validate only arg names; validate arg values and reference targets too.
4. Do not produce paths like `statements[?]`.
5. Do not hardcode semantic diagnostics to `statements[0]`.
6. Do not ignore `source_name` in parse diagnostics.
7. Do not implement two different validation systems with different behavior.
8. Do not put real validation in a non-exported helper while exported `ValidationVisitor` is broken.
9. Do not manually duplicate language definitions in schema and loader.
10. Do not omit nested `accept()` methods.
11. Do not leave `jsonschema` out of test dependencies if tests import it.
12. Do not commit `__pycache__` files.

## Completion Checklist

Before saying the work is complete, run this checklist manually:

- `workflow_engine.__all__` exports the public functions, result types, AST nodes, visitors, diagnostics.
- `parse_yaml` returns `ParseResult`.
- `load_yaml` returns `ParseResult`.
- `build_ast` returns `AstBuildResult`.
- `validate` returns `ValidationResult`.
- malformed YAML returns diagnostic, not exception.
- malformed nested validation call returns diagnostic, not exception.
- malformed nested operation call returns diagnostic, not exception.
- missing transition required field prevents AST document construction.
- validation catches unknown refs in call args.
- validation catches wrong scalar arg types.
- validation catches `many` list/scalar mistakes.
- no diagnostic path contains `?`.
- line/column are one-based.
- schema is generated from `langdef.py`.
- tests include positive, negative, schema, visitor, and no-crash probes.
- full test suite passes.

## Acceptance Criteria

The implementation satisfies this spec when:

1. The `workflow_engine` package is standalone.
2. The public API matches this spec.
3. YAML parses into an immutable AST.
4. Shape errors return diagnostics and prevent invalid AST construction.
5. Semantic validation uses symbol tables and visitors.
6. Validation checks references, arg names, arg types, and `many`.
7. JSON Schema is generated from shared language definitions.
8. Source locations are attached to AST nodes and diagnostics where possible.
9. Tests cover all required cases.
10. `uv run pytest -q` passes.
