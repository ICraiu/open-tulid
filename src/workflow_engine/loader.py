from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import langdef
from .ast import (
    ArgSpec,
    ArtifactTypeStatement,
    ObsidianStateMapping,
    ObsidianStorage,
    OperationCall,
    OperationTypeStatement,
    RequirementSet,
    StateStatement,
    Statement,
    TaskTypeStatement,
    TransactionPlan,
    TransitionStatement,
    ValidationCall,
    ValidationTypeStatement,
    WorkerStatement,
    WorkflowStorage,
    WorkflowDocument,
)
from .diagnostics import Diagnostic, SourceSpan


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.default_flow_style = False
    return y


def _lc_to_span(lc, path: str) -> SourceSpan | None:
    """Convert a ruamel.yaml (line, col) tuple to a one-based SourceSpan."""
    if lc is None:
        return None
    line, col = lc
    return SourceSpan(
        path=path,
        line=line + 1,
        column=col + 1,
    )


def _node_lc(node):
    """Get location of a node (CommentedMap/CommentedSeq)."""
    if isinstance(node, (CommentedMap, CommentedSeq)):
        return (node.lc.line, node.lc.col)
    return None


def _key_lc(mapping, key):
    """Get location of a key within a CommentedMap."""
    if isinstance(mapping, CommentedMap):
        try:
            return mapping.lc.key(key)
        except (AttributeError, KeyError, TypeError):
            pass
    return _node_lc(mapping)


def _list_item_lc(sequence, index):
    """Get location of an item within a CommentedSeq."""
    if isinstance(sequence, CommentedSeq):
        try:
            return sequence.lc.item(index)
        except (AttributeError, IndexError, TypeError):
            pass
    return _node_lc(sequence)


def node_span(node, path: str) -> SourceSpan | None:
    """Centralized helper: get SourceSpan for a raw YAML node."""
    lc = _node_lc(node)
    return _lc_to_span(lc, path) if lc else None


def key_span(mapping, key, path: str) -> SourceSpan | None:
    """Centralized helper: get SourceSpan for a key in a mapping."""
    lc = _key_lc(mapping, key)
    return _lc_to_span(lc, path) if lc else None


def list_item_span(sequence, index, path: str) -> SourceSpan | None:
    """Centralized helper: get SourceSpan for a list item."""
    lc = _list_item_lc(sequence, index)
    return _lc_to_span(lc, path) if lc else None


def _is_scalar_bool(val) -> bool:
    return isinstance(val, bool)


def _is_yaml_int(val) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _diag_from_span(code: str, message: str, span: SourceSpan | None) -> Diagnostic:
    """Create a Diagnostic from a SourceSpan, extracting line/column."""
    return Diagnostic(
        code=code,
        message=message,
        path=span.path if span else None,
        line=span.line if span else None,
        column=span.column if span else None,
    )


def parse_yaml(source: str, *, source_name: str = "<memory>") -> ParseResult:
    from . import ParseResult as PR
    try:
        y = _yaml()
        parsed = y.load(source)
        return PR(value=parsed, diagnostics=())
    except Exception as e:
        diag = Diagnostic(
            code="workflow.yaml.parse_error",
            message=str(e),
            severity="error",
            path=source_name,
        )
        return PR(value=None, diagnostics=(diag,))


def load_yaml(path: str | Path) -> ParseResult:
    from . import ParseResult as PR
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        diag = Diagnostic(
            code="workflow.yaml.parse_error",
            message=f"cannot read file: {e}",
            severity="error",
            path=str(p),
        )
        return PR(value=None, diagnostics=(diag,))
    return parse_yaml(text, source_name=str(p))


def build_ast(parsed: object | None, *, source_name: str = "<memory>") -> AstBuildResult:
    from . import AstBuildResult as ABR
    if parsed is None:
        return ABR(
            document=None,
            diagnostics=(
                Diagnostic(
                    code="workflow.shape.root_not_mapping",
                    message="parsed document is None",
                    path=source_name,
                ),
            ),
        )

    if not isinstance(parsed, dict):
        return ABR(
            document=None,
            diagnostics=(
                Diagnostic(
                    code="workflow.shape.root_not_mapping",
                    message=f"root must be a mapping, got {type(parsed).__name__}",
                    path=source_name,
                ),
            ),
        )

    diagnostics: list[Diagnostic] = []

    # Unknown top-level keys - use YAML key path, not source_name
    for key in parsed:
        if key not in ("schema_version", "statements", "storage"):
            span = key_span(parsed, key, str(key))
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown top-level key: {key!r}",
                span,
            ))

    # schema_version checks
    if "schema_version" not in parsed:
        span = key_span(parsed, "schema_version", "schema_version")
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            "missing required field: schema_version",
            span,
        ))
        return ABR(document=None, diagnostics=tuple(diagnostics))

    sv = parsed["schema_version"]
    if _is_scalar_bool(sv) or not _is_yaml_int(sv):
        span = key_span(parsed, "schema_version", "schema_version")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"schema_version must be an integer, got {type(sv).__name__}",
            span,
        ))
        return ABR(document=None, diagnostics=tuple(diagnostics))

    if sv not in langdef.SUPPORTED_SCHEMA_VERSIONS:
        span = key_span(parsed, "schema_version", "schema_version")
        diagnostics.append(_diag_from_span(
            "workflow.schema.unsupported_version",
            f"unsupported schema_version: {sv}",
            span,
        ))
        return ABR(document=None, diagnostics=tuple(diagnostics))

    # statements checks
    if "statements" not in parsed:
        span = key_span(parsed, "statements", "statements")
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            "missing required field: statements",
            span,
        ))
        return ABR(document=None, diagnostics=tuple(diagnostics))

    stmts_raw = parsed["statements"]
    if not isinstance(stmts_raw, list):
        span = key_span(parsed, "statements", "statements")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"statements must be a list, got {type(stmts_raw).__name__}",
            span,
        ))
        return ABR(document=None, diagnostics=tuple(diagnostics))

    doc_lc = _node_lc(parsed)
    doc_span = _lc_to_span(doc_lc, source_name) if doc_lc else None
    statements: list[Statement] = []

    for idx, stmt_raw in enumerate(stmts_raw):
        item_path = f"statements[{idx}]"
        item_span = list_item_span(stmts_raw, idx, item_path)

        stmt_diags, stmt = _build_statement(stmt_raw, idx, item_path, item_span, source_name)
        diagnostics.extend(stmt_diags)
        if stmt is not None:
            statements.append(stmt)

    if diagnostics:
        return ABR(document=None, diagnostics=tuple(diagnostics))

    storage: WorkflowStorage | None = None
    if "storage" in parsed:
        storage_diags, storage = _build_storage(parsed["storage"])
        diagnostics.extend(storage_diags)

    if diagnostics:
        return ABR(document=None, diagnostics=tuple(diagnostics))

    document = WorkflowDocument(
        schema_version=int(sv),
        statements=tuple(statements),
        storage=storage,
        span=doc_span,
    )
    return ABR(document=document, diagnostics=())


def _build_statement(
    raw: object,
    idx: int,
    item_path: str,
    item_span: SourceSpan | None,
    source_name: str,
) -> tuple[list[Diagnostic], Statement | None]:
    diagnostics: list[Diagnostic] = []

    if not isinstance(raw, dict):
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"statement at index {idx} must be a mapping, got {type(raw).__name__}",
            item_span,
        ))
        return diagnostics, None

    if "kind" not in raw:
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            f"statement at index {idx} missing required field: kind",
            item_span,
        ))
        return diagnostics, None

    kind = raw["kind"]
    kind_path = f"{item_path}.kind"
    kind_span = key_span(raw, "kind", kind_path)

    if not isinstance(kind, str):
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"kind must be a string, got {type(kind).__name__}",
            kind_span,
        ))
        return diagnostics, None

    if kind not in langdef.SUPPORTED_KINDS:
        diagnostics.append(_diag_from_span(
            "workflow.statement.unknown_kind",
            f"unknown statement kind: {kind!r}",
            kind_span,
        ))
        return diagnostics, None

    if "id" not in raw:
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            f"statement at index {idx} missing required field: id",
            item_span,
        ))
        return diagnostics, None

    stmt_id = raw["id"]
    id_path = f"{item_path}.id"
    id_span = key_span(raw, "id", id_path)

    if not isinstance(stmt_id, str):
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"id must be a string, got {type(stmt_id).__name__}",
            id_span,
        ))
        return diagnostics, None

    # Check unknown keys
    allowed_keys = langdef.STATEMENT_KEYS[kind]
    for key in raw:
        if key not in allowed_keys:
            key_sp = key_span(raw, key, f"{item_path}.{key}")
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown key {key!r} in {kind} statement",
                key_sp,
            ))

    # Build field_spans for semantic validation
    field_spans: dict[str, SourceSpan] = {}
    for field_name in ("task_type", "from", "to", "worker", "default_for_scheduler"):
        if field_name in raw:
            fs = key_span(raw, field_name, f"{item_path}.{field_name}")
            if fs:
                field_spans[field_name] = fs

    if kind == "state":
        return diagnostics, StateStatement(id=stmt_id, span=item_span)

    if kind == "artifact_type":
        template = raw.get("template")
        if template is not None and not isinstance(template, str):
            t_span = key_span(raw, "template", f"{item_path}.template")
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "template must be a string",
                t_span,
            ))
            return diagnostics, None
        return diagnostics, ArtifactTypeStatement(id=stmt_id, template=template, span=item_span)

    if kind == "worker":
        wtype = raw.get("type")
        if wtype is not None and not isinstance(wtype, str):
            t_span = key_span(raw, "type", f"{item_path}.type")
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "type must be a string",
                t_span,
            ))
            return diagnostics, None
        return diagnostics, WorkerStatement(id=stmt_id, type=wtype, span=item_span)

    if kind == "task_type":
        return _build_task_type(raw, stmt_id, item_path, item_span, source_name)

    if kind == "validation_type":
        return _build_validation_type(raw, stmt_id, item_path, item_span, source_name)

    if kind == "operation_type":
        return _build_operation_type(raw, stmt_id, item_path, item_span, source_name)

    if kind == "transition":
        return _build_transition(raw, stmt_id, idx, item_path, item_span, field_spans, source_name)

    return diagnostics, None


def _build_task_type(
    raw: dict,
    stmt_id: str,
    item_path: str,
    item_span: SourceSpan | None,
    source_name: str,
) -> tuple[list[Diagnostic], Statement | None]:
    diagnostics: list[Diagnostic] = []
    requirements_raw = raw.get("requirements")

    if requirements_raw is None:
        return diagnostics, TaskTypeStatement(id=stmt_id, span=item_span)

    if not isinstance(requirements_raw, dict):
        req_span = key_span(raw, "requirements", f"{item_path}.requirements")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "requirements must be a mapping",
            req_span,
        ))
        return diagnostics, None

    # Validate requirement keys are strings
    for state_name in requirements_raw:
        if not isinstance(state_name, str):
            req_key_path = f"{item_path}.requirements"
            # Try to get span of the key itself
            key_sp = key_span(requirements_raw, state_name, req_key_path)
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                f"requirement key must be a string, got {type(state_name).__name__}",
                key_sp,
            ))
            return diagnostics, None

    reqs_by_state: dict[str, RequirementSet] = {}
    for state_name, req_raw in requirements_raw.items():
        req_path = f"{item_path}.requirements.{state_name}"
        diag, req_set = _build_requirement_set(req_raw, state_name, req_path, source_name)
        diagnostics.extend(diag)
        if req_set is not None:
            reqs_by_state[state_name] = req_set

    if diagnostics:
        return diagnostics, None

    return diagnostics, TaskTypeStatement(id=stmt_id, requirements_by_state=reqs_by_state, span=item_span)


def _build_validation_type(
    raw: dict,
    stmt_id: str,
    item_path: str,
    item_span: SourceSpan | None,
    source_name: str,
) -> tuple[list[Diagnostic], Statement | None]:
    diagnostics: list[Diagnostic] = []
    args_raw = raw.get("args")

    if args_raw is None:
        return diagnostics, ValidationTypeStatement(id=stmt_id, span=item_span)

    if not isinstance(args_raw, dict):
        args_span = key_span(raw, "args", f"{item_path}.args")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "args must be a mapping",
            args_span,
        ))
        return diagnostics, None

    args: dict[str, ArgSpec] = {}
    for arg_name, arg_raw in args_raw.items():
        arg_path = f"{item_path}.args.{arg_name}"
        diag, arg_spec = _build_arg_spec(arg_raw, arg_name, arg_path, source_name)
        diagnostics.extend(diag)
        if arg_spec is not None:
            args[arg_name] = arg_spec

    if diagnostics:
        return diagnostics, None

    return diagnostics, ValidationTypeStatement(id=stmt_id, args=args, span=item_span)


def _build_operation_type(
    raw: dict,
    stmt_id: str,
    item_path: str,
    item_span: SourceSpan | None,
    source_name: str,
) -> tuple[list[Diagnostic], Statement | None]:
    diagnostics: list[Diagnostic] = []
    args_raw = raw.get("args")

    if args_raw is None:
        return diagnostics, OperationTypeStatement(id=stmt_id, span=item_span)

    if not isinstance(args_raw, dict):
        args_span = key_span(raw, "args", f"{item_path}.args")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "args must be a mapping",
            args_span,
        ))
        return diagnostics, None

    args: dict[str, ArgSpec] = {}
    for arg_name, arg_raw in args_raw.items():
        arg_path = f"{item_path}.args.{arg_name}"
        diag, arg_spec = _build_arg_spec(arg_raw, arg_name, arg_path, source_name)
        diagnostics.extend(diag)
        if arg_spec is not None:
            args[arg_name] = arg_spec

    if diagnostics:
        return diagnostics, None

    return diagnostics, OperationTypeStatement(id=stmt_id, args=args, span=item_span)


def _build_arg_spec(
    raw: object,
    arg_name: str,
    arg_path: str,
    source_name: str,
) -> tuple[list[Diagnostic], ArgSpec | None]:
    diagnostics: list[Diagnostic] = []

    if not isinstance(raw, dict):
        span = node_span(raw, arg_path)
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"arg spec for {arg_name!r} must be a mapping",
            span,
        ))
        return diagnostics, None

    # Unknown keys in arg spec
    for key in raw:
        if key not in langdef.ARG_SPEC_KEYS:
            ks = key_span(raw, key, f"{arg_path}.{key}")
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown key {key!r} in arg spec for {arg_name!r}",
                ks,
            ))

    if "type" not in raw:
        span = node_span(raw, arg_path)
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            f"arg spec for {arg_name!r} missing required field: type",
            span,
        ))
        return diagnostics, None

    arg_type = raw["type"]
    if not isinstance(arg_type, str):
        type_span = key_span(raw, "type", f"{arg_path}.type")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"arg spec type must be a string",
            type_span,
        ))
        return diagnostics, None

    if arg_type not in langdef.SUPPORTED_ARG_TYPES:
        type_span = key_span(raw, "type", f"{arg_path}.type")
        diagnostics.append(_diag_from_span(
            "workflow.call.unknown_argument_type",
            f"unknown arg spec type: {arg_type!r}",
            type_span,
        ))
        return diagnostics, None

    required = raw.get("required", False)
    if not isinstance(required, bool):
        req_span = key_span(raw, "required", f"{arg_path}.required")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"arg spec required must be a boolean",
            req_span,
        ))
        return diagnostics, None

    many = raw.get("many", False)
    if not isinstance(many, bool):
        many_span = key_span(raw, "many", f"{arg_path}.many")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"arg spec many must be a boolean",
            many_span,
        ))
        return diagnostics, None

    span = node_span(raw, arg_path)
    return diagnostics, ArgSpec(type=arg_type, required=required, many=many, span=span)


def _build_transition(
    raw: dict,
    stmt_id: str,
    idx: int,
    item_path: str,
    item_span: SourceSpan | None,
    field_spans: Mapping[str, SourceSpan],
    source_name: str,
) -> tuple[list[Diagnostic], Statement | None]:
    diagnostics: list[Diagnostic] = []

    # Check required transition fields
    for req_key in langdef.TRANSITION_REQUIRED_KEYS - {"kind", "id"}:
        if req_key not in raw:
            field_path = f"{item_path}.{req_key}"
            field_span = field_spans.get(req_key)
            if field_span is None:
                field_span = key_span(raw, req_key, field_path)
            diagnostics.append(_diag_from_span(
                "workflow.shape.missing_required_field",
                f"transition missing required field: {req_key}",
                field_span,
            ))

    if diagnostics:
        return diagnostics, None

    task_type = raw["task_type"]
    if not isinstance(task_type, str):
        tt_span = key_span(raw, "task_type", f"{item_path}.task_type")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "task_type must be a string",
            tt_span,
        ))
        return diagnostics, None

    from_state = raw["from"]
    if not isinstance(from_state, str):
        f_span = key_span(raw, "from", f"{item_path}.from")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "from must be a string",
            f_span,
        ))
        return diagnostics, None

    to_state = raw["to"]
    if not isinstance(to_state, str):
        t_span = key_span(raw, "to", f"{item_path}.to")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "to must be a string",
            t_span,
        ))
        return diagnostics, None

    worker = raw.get("worker")
    if worker is not None and not isinstance(worker, str):
        w_span = key_span(raw, "worker", f"{item_path}.worker")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "worker must be a string",
            w_span,
        ))
        return diagnostics, None

    default_for_scheduler = raw.get("default_for_scheduler", False)
    if not isinstance(default_for_scheduler, bool):
        default_span = key_span(raw, "default_for_scheduler", f"{item_path}.default_for_scheduler")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "default_for_scheduler must be a boolean",
            default_span,
        ))
        return diagnostics, None

    requires = RequirementSet()
    requires_raw = raw.get("requires")
    if requires_raw is not None:
        req_path = f"{item_path}.requires"
        diag, req_set = _build_requirement_set(requires_raw, "transition", req_path, source_name)
        diagnostics.extend(diag)
        if req_set is not None:
            requires = req_set

    transaction = None
    txn_raw = raw.get("transaction")
    if txn_raw is not None:
        txn_path = f"{item_path}.transaction"
        diag, txn = _build_transaction(txn_raw, txn_path, source_name)
        diagnostics.extend(diag)
        if txn is not None:
            transaction = txn

    if diagnostics:
        return diagnostics, None

    return diagnostics, TransitionStatement(
        id=stmt_id,
        task_type=task_type,
        from_state=from_state,
        to_state=to_state,
        worker=worker,
        default_for_scheduler=default_for_scheduler,
        requires=requires,
        transaction=transaction,
        span=item_span,
        field_spans=dict(field_spans),
    )


def _build_storage(raw: object) -> tuple[list[Diagnostic], WorkflowStorage | None]:
    diagnostics: list[Diagnostic] = []
    if raw is None:
        return diagnostics, None
    if not isinstance(raw, dict):
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "storage must be a mapping",
            node_span(raw, "storage"),
        ))
        return diagnostics, None

    for key in raw:
        if key != "obsidian":
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown storage key: {key!r}",
                key_span(raw, key, f"storage.{key}"),
            ))

    obsidian: ObsidianStorage | None = None
    if "obsidian" in raw:
        obsidian_diags, obsidian = _build_obsidian_storage(raw["obsidian"])
        diagnostics.extend(obsidian_diags)

    if diagnostics:
        return diagnostics, None
    return diagnostics, WorkflowStorage(
        obsidian=obsidian,
        span=node_span(raw, "storage"),
    )


def _build_obsidian_storage(raw: object) -> tuple[list[Diagnostic], ObsidianStorage | None]:
    diagnostics: list[Diagnostic] = []
    if not isinstance(raw, dict):
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "storage.obsidian must be a mapping",
            node_span(raw, "storage.obsidian"),
        ))
        return diagnostics, None

    for key in raw:
        if key not in {"boards", "state_mappings"}:
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown storage.obsidian key: {key!r}",
                key_span(raw, key, f"storage.obsidian.{key}"),
            ))

    boards_raw = raw.get("boards", {})
    if not isinstance(boards_raw, dict):
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "storage.obsidian.boards must be a mapping",
            key_span(raw, "boards", "storage.obsidian.boards"),
        ))
        return diagnostics, None
    boards: dict[str, str] = {}
    for name, path in boards_raw.items():
        if not isinstance(name, str) or not isinstance(path, str) or not name.strip() or not path.strip():
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "storage.obsidian.boards entries must map non-empty string board names to non-empty string paths",
                key_span(boards_raw, name, f"storage.obsidian.boards.{name}"),
            ))
            continue
        boards[name.strip()] = path.strip()

    mappings_raw = raw.get("state_mappings", ())
    if not isinstance(mappings_raw, list):
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "storage.obsidian.state_mappings must be a list",
            key_span(raw, "state_mappings", "storage.obsidian.state_mappings"),
        ))
        return diagnostics, None

    mappings: list[ObsidianStateMapping] = []
    for index, item in enumerate(mappings_raw):
        item_path = f"storage.obsidian.state_mappings[{index}]"
        item_span = list_item_span(mappings_raw, index, item_path)
        if not isinstance(item, dict):
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "storage.obsidian.state_mappings entries must be mappings",
                item_span,
            ))
            continue
        for key in item:
            if key not in {"state", "board", "column"}:
                diagnostics.append(_diag_from_span(
                    "workflow.shape.unknown_key",
                    f"unknown state mapping key: {key!r}",
                    key_span(item, key, f"{item_path}.{key}"),
                ))
        state = item.get("state")
        board = item.get("board")
        column = item.get("column")
        if not all(isinstance(value, str) and value.strip() for value in (state, board, column)):
            diagnostics.append(_diag_from_span(
                "workflow.shape.missing_required_field",
                "state mappings require non-empty string state, board, and column",
                item_span,
            ))
            continue
        mappings.append(ObsidianStateMapping(
            state=state.strip(),
            board=board.strip(),
            column=column.strip(),
            span=item_span,
        ))

    if diagnostics:
        return diagnostics, None
    return diagnostics, ObsidianStorage(
        boards=boards,
        state_mappings=tuple(mappings),
        span=node_span(raw, "storage.obsidian"),
    )


def _build_requirement_set(
    raw: object,
    context: str,
    req_path: str,
    source_name: str,
) -> tuple[list[Diagnostic], RequirementSet | None]:
    diagnostics: list[Diagnostic] = []

    if not isinstance(raw, dict):
        span = node_span(raw, req_path)
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"requirement set must be a mapping",
            span,
        ))
        return diagnostics, None

    for key in raw:
        if key not in langdef.REQUIREMENT_SET_KEYS:
            ks = key_span(raw, key, f"{req_path}.{key}")
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown key {key!r} in requirement set",
                ks,
            ))

    artifacts: list[str] = []
    artifact_spans: list[SourceSpan | None] = []
    arts_raw = raw.get("artifacts")
    if arts_raw is not None:
        if not isinstance(arts_raw, list):
            art_span = key_span(raw, "artifacts", f"{req_path}.artifacts")
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "artifacts must be a list",
                art_span,
            ))
            return diagnostics, None
        for i, art in enumerate(arts_raw):
            if not isinstance(art, str):
                item_sp = list_item_span(arts_raw, i, f"{req_path}.artifacts[{i}]")
                diagnostics.append(_diag_from_span(
                    "workflow.shape.wrong_type",
                    f"artifact item must be a string, got {type(art).__name__}",
                    item_sp,
                ))
                return diagnostics, None
            artifacts.append(art)
            artifact_spans.append(list_item_span(arts_raw, i, f"{req_path}.artifacts[{i}]"))

    validations: list[ValidationCall] = []
    vals_raw = raw.get("validations")
    if vals_raw is not None:
        if not isinstance(vals_raw, list):
            val_span = key_span(raw, "validations", f"{req_path}.validations")
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "validations must be a list",
                val_span,
            ))
            return diagnostics, None
        for i, val_raw in enumerate(vals_raw):
            val_path = f"{req_path}.validations[{i}]"
            val_item_span = list_item_span(vals_raw, i, val_path)
            diag, val_call = _build_validation_call(val_raw, val_path, val_item_span, source_name)
            diagnostics.extend(diag)
            if val_call is not None:
                validations.append(val_call)

    if diagnostics:
        return diagnostics, None

    span = node_span(raw, req_path)
    return diagnostics, RequirementSet(
        artifacts=tuple(artifacts),
        validations=tuple(validations),
        span=span,
        artifact_spans=tuple(artifact_spans),
    )


def _build_validation_call(
    raw: object,
    val_path: str,
    item_span: SourceSpan | None,
    source_name: str,
) -> tuple[list[Diagnostic], ValidationCall | None]:
    diagnostics: list[Diagnostic] = []

    if not isinstance(raw, dict):
        span = node_span(raw, val_path) or item_span
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"validation call must be a mapping, got {type(raw).__name__}",
            span,
        ))
        return diagnostics, None

    for key in raw:
        if key not in langdef.VALIDATION_CALL_KEYS:
            ks = key_span(raw, key, f"{val_path}.{key}")
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown key {key!r} in validation call",
                ks,
            ))

    if "type" not in raw:
        span = node_span(raw, val_path)
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            "validation call missing required field: type",
            span,
        ))
        return diagnostics, None

    val_type = raw["type"]
    if not isinstance(val_type, str):
        type_span = key_span(raw, "type", f"{val_path}.type")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "validation call type must be a string",
            type_span,
        ))
        return diagnostics, None

    args = {}
    arg_spans: dict[str, SourceSpan] = {}
    args_raw = raw.get("args")
    if args_raw is not None:
        if not isinstance(args_raw, dict):
            args_span = key_span(raw, "args", f"{val_path}.args")
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "validation call args must be a mapping",
                args_span,
            ))
            return diagnostics, None
        args = dict(args_raw)
        for arg_name in args_raw:
            span = key_span(args_raw, arg_name, f"{val_path}.args.{arg_name}")
            if span:
                arg_spans[arg_name] = span

    span = node_span(raw, val_path)
    return diagnostics, ValidationCall(type=val_type, args=args, span=span, arg_spans=arg_spans)


def _build_transaction(
    raw: object,
    txn_path: str,
    source_name: str,
) -> tuple[list[Diagnostic], TransactionPlan | None]:
    diagnostics: list[Diagnostic] = []

    if not isinstance(raw, dict):
        span = node_span(raw, txn_path)
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "transaction must be a mapping",
            span,
        ))
        return diagnostics, None

    for key in raw:
        if key not in langdef.TRANSACTION_PLAN_KEYS:
            ks = key_span(raw, key, f"{txn_path}.{key}")
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown key {key!r} in transaction",
                ks,
            ))

    if "steps" not in raw:
        span = node_span(raw, txn_path)
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            "transaction missing required field: steps",
            span,
        ))
        return diagnostics, None

    steps_raw = raw["steps"]
    if not isinstance(steps_raw, list):
        steps_span = key_span(raw, "steps", f"{txn_path}.steps")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "transaction steps must be a list",
            steps_span,
        ))
        return diagnostics, None

    steps: list[OperationCall] = []
    for i, step_raw in enumerate(steps_raw):
        step_path = f"{txn_path}.steps[{i}]"
        step_item_span = list_item_span(steps_raw, i, step_path)
        diag, op_call = _build_operation_call(step_raw, step_path, step_item_span, source_name)
        diagnostics.extend(diag)
        if op_call is not None:
            steps.append(op_call)

    if diagnostics:
        return diagnostics, None

    span = node_span(raw, txn_path)
    return diagnostics, TransactionPlan(steps=tuple(steps), span=span)


def _build_operation_call(
    raw: object,
    step_path: str,
    item_span: SourceSpan | None,
    source_name: str,
) -> tuple[list[Diagnostic], OperationCall | None]:
    diagnostics: list[Diagnostic] = []

    if not isinstance(raw, dict):
        span = node_span(raw, step_path) or item_span
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            f"operation call must be a mapping, got {type(raw).__name__}",
            span,
        ))
        return diagnostics, None

    for key in raw:
        if key not in langdef.OPERATION_CALL_KEYS:
            ks = key_span(raw, key, f"{step_path}.{key}")
            diagnostics.append(_diag_from_span(
                "workflow.shape.unknown_key",
                f"unknown key {key!r} in operation call",
                ks,
            ))

    if "op" not in raw:
        span = node_span(raw, step_path)
        diagnostics.append(_diag_from_span(
            "workflow.shape.missing_required_field",
            "operation call missing required field: op",
            span,
        ))
        return diagnostics, None

    op = raw["op"]
    if not isinstance(op, str):
        op_span = key_span(raw, "op", f"{step_path}.op")
        diagnostics.append(_diag_from_span(
            "workflow.shape.wrong_type",
            "operation call op must be a string",
            op_span,
        ))
        return diagnostics, None

    args = {}
    arg_spans: dict[str, SourceSpan] = {}
    args_raw = raw.get("args")
    if args_raw is not None:
        if not isinstance(args_raw, dict):
            args_span = key_span(raw, "args", f"{step_path}.args")
            diagnostics.append(_diag_from_span(
                "workflow.shape.wrong_type",
                "operation call args must be a mapping",
                args_span,
            ))
            return diagnostics, None
        args = dict(args_raw)
        for arg_name in args_raw:
            span = key_span(args_raw, arg_name, f"{step_path}.args.{arg_name}")
            if span:
                arg_spans[arg_name] = span

    span = node_span(raw, step_path)
    return diagnostics, OperationCall(op=op, args=args, span=span, arg_spans=arg_spans)
