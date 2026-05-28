from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ruamel.yaml import YAML

from open_tulid.domain.schema import (
    BoardPosition,
    DomainError,
    ProjectSnapshot,
    StorageDefinition,
    Task,
    WorkflowDefinition,
)
from open_tulid.vault.links import parse_task_row

from .base import (
    AdapterCapability,
    LoadProjectResult,
    ReadTaskResult,
    WriteResult,
)


ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
NUMERIC_TASK_ID_RE = re.compile(r"^[1-9][0-9]*$")


def _yaml() -> YAML:
    yaml = YAML()
    yaml.default_flow_style = False
    return yaml


@dataclass(frozen=True)
class ObsidianStateMapping:
    state: str
    board: str
    column: str


@dataclass(frozen=True)
class ObsidianAdapterConfig:
    project_id: str
    project_root: Path
    boards: Mapping[str, str | Path]
    state_mappings: tuple[ObsidianStateMapping, ...]
    tasks_dir: str | Path = "tasks"
    events_dir: str | Path = "events"


@dataclass(frozen=True)
class _TaskDocument:
    task: Task
    frontmatter: Mapping[str, Any]


@dataclass(frozen=True)
class _BoardCard:
    board: str
    board_path: Path
    column: str
    line_index: int
    card_text: str
    task_note: str


@dataclass(frozen=True)
class _BoardDocument:
    board: str
    path: Path
    lines: tuple[str, ...]
    cards: tuple[_BoardCard, ...]
    insert_after_by_column: Mapping[str, int]


@dataclass
class _LoadedProject:
    snapshot: ProjectSnapshot | None = None
    task_paths_by_id: dict[str, Path] = field(default_factory=dict)
    task_notes_by_id: dict[str, str] = field(default_factory=dict)
    cards_by_task_id: dict[str, _BoardCard] = field(default_factory=dict)
    card_seen_task_ids: set[str] = field(default_factory=set)
    boards_by_name: dict[str, _BoardDocument] = field(default_factory=dict)
    errors: list[DomainError] = field(default_factory=list)


@dataclass(frozen=True)
class _PreparedWrite:
    path: Path
    content: str


@dataclass(frozen=True)
class _TaskStateCacheUpdate:
    content: str | None = None
    error: DomainError | None = None


class ObsidianAdapter:
    name = "obsidian"
    capabilities = frozenset({
        AdapterCapability.LOAD_PROJECT,
        AdapterCapability.READ_TASK,
        AdapterCapability.WRITE_TASK,
        AdapterCapability.CREATE_TASK,
        AdapterCapability.MOVE_TASK,
        AdapterCapability.APPEND_EVENT,
    })

    def __init__(self, config: ObsidianAdapterConfig):
        self.config = config

    def load_project(self) -> LoadProjectResult:
        loaded = self._load()
        if loaded.errors:
            return LoadProjectResult(errors=tuple(loaded.errors))
        return LoadProjectResult(snapshot=loaded.snapshot)

    def read_task(self, task_id: str) -> ReadTaskResult:
        loaded = self._load()
        if loaded.errors:
            return ReadTaskResult(errors=tuple(loaded.errors))
        if loaded.snapshot is None or task_id not in loaded.snapshot.tasks:
            return ReadTaskResult(errors=(_error(
                "task.not_found",
                f"Task {task_id} was not found.",
                task_id,
            ),))
        return ReadTaskResult(task=loaded.snapshot.tasks[task_id])

    def write_task(self, task: Task) -> WriteResult:
        loaded = self._load()
        if loaded.errors:
            return WriteResult(errors=tuple(loaded.errors))

        task_path = loaded.task_paths_by_id.get(task.id)
        if task_path is None:
            task_path = self._tasks_root() / f"{task.id}-{_slugify(task.title)}.md"

        return self._write_text(task_path, _serialize_task(task))

    def create_task(self, task: Task) -> WriteResult:
        loaded = self._load()
        if loaded.errors:
            return WriteResult(errors=tuple(loaded.errors))
        if task.id in loaded.task_paths_by_id:
            return WriteResult(errors=(_error(
                "task.already_exists",
                f"Task {task.id} already exists.",
                task.id,
            ),))
        target = self._mapping_for_state(task.current_state)
        if target is None:
            return WriteResult(errors=(_error(
                "state.unmapped",
                f"State {task.current_state!r} is not mapped to an Obsidian board column.",
                task.current_state,
            ),))
        board = loaded.boards_by_name.get(target.board)
        if board is None:
            return WriteResult(errors=(_error(
                "board.not_found",
                f"Board {target.board!r} was not loaded.",
                target.board,
            ),))
        insert_at = _find_column_insert_index(list(board.lines), target.column)
        if insert_at is None:
            return WriteResult(errors=(_error(
                "board.column_not_found",
                f"Board {target.board!r} does not contain column {target.column!r}.",
                target.column,
            ),))
        task_path = self._tasks_root() / f"{task.id}-{_slugify(task.title)}.md"
        note_name = task_path.stem
        task_content = _serialize_task(task)
        board_lines = list(board.lines)
        board_lines.insert(insert_at, f"- [ ] [[{note_name}]]")
        return self._write_prepared([
            _PreparedWrite(path=task_path, content=task_content),
            _PreparedWrite(path=board.path, content="\n".join(board_lines) + "\n"),
        ])

    def move_task(self, task_id: str, state: str) -> WriteResult:
        loaded = self._load()
        if loaded.errors:
            return WriteResult(errors=tuple(loaded.errors))

        card = loaded.cards_by_task_id.get(task_id)
        if card is None:
            return WriteResult(errors=(_error(
                "task.card_not_found",
                f"Task {task_id} does not have a board card.",
                task_id,
            ),))

        target = self._mapping_for_state(state)
        if target is None:
            return WriteResult(errors=(_error(
                "state.unmapped",
                f"State {state!r} is not mapped to an Obsidian board column.",
                state,
            ),))

        if card.board == target.board and card.column == target.column:
            return self._sync_task_state_cache(task_id, loaded, state, fallback_path=card.board_path)

        source_board = loaded.boards_by_name[card.board]
        if target.board != card.board:
            return WriteResult(errors=(_error(
                "adapter.cross_board_move_unsupported",
                "Moving cards between different board files is not implemented yet.",
                task_id,
            ),))

        lines = list(source_board.lines)
        moved_line = lines.pop(card.line_index)
        insert_at = _find_column_insert_index(lines, target.column)
        if insert_at is None:
            return WriteResult(errors=(_error(
                "board.column_not_found",
                f"Board {target.board!r} does not contain column {target.column!r}.",
                target.column,
            ),))
        lines.insert(insert_at, moved_line)

        writes: list[_PreparedWrite] = []
        task_path = loaded.task_paths_by_id.get(task_id)
        if task_path is not None:
            cache_update = self._prepare_task_state_cache(task_path, state)
            if cache_update.error is not None:
                return WriteResult(errors=(cache_update.error,))
            if cache_update.content is not None:
                writes.append(_PreparedWrite(path=task_path, content=cache_update.content))

        writes.append(_PreparedWrite(path=source_board.path, content="\n".join(lines) + "\n"))
        board_write = self._write_prepared(writes)
        if not board_write.accepted:
            return board_write
        return board_write

    def append_event(self, event: Mapping[str, Any]) -> WriteResult:
        event_date = _event_date(event)
        event_path = self._events_root() / f"{event_date}.jsonl"
        line = json.dumps(dict(event), sort_keys=True, separators=(",", ":")) + "\n"
        try:
            event_path.parent.mkdir(parents=True, exist_ok=True)
            with event_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            return WriteResult(errors=(_error(
                "event.write_failed",
                f"Cannot append event: {exc}",
                str(event_path),
            ),))
        return WriteResult(path=str(event_path))

    def repair_project(self, *, fix: bool = False) -> tuple[DomainError, ...]:
        errors: list[DomainError] = []
        note_states: dict[str, str] = {}
        for board_name, board_path_value in self.config.boards.items():
            board_path = self._project_path(board_path_value)
            board_doc = self._read_board(board_name, board_path)
            if isinstance(board_doc, DomainError):
                errors.append(board_doc)
                continue
            for card in board_doc.cards:
                state = self._state_for_board_column(card.board, card.column)
                if state is None:
                    errors.append(_error(
                        "board.column_unmapped",
                        f"Board {card.board!r} column {card.column!r} is not mapped to a logical state.",
                        f"{board_path}:{card.line_index + 1}",
                    ))
                    continue
                note_states[card.task_note] = state

        used_numeric_ids = _existing_numeric_task_ids(self._tasks_root())
        next_numeric_id = max(used_numeric_ids, default=0) + 1
        writes: list[_PreparedWrite] = []

        for task_path in sorted(self._tasks_root().glob("*.md")):
            try:
                content = task_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                errors.append(_error("task.invalid_utf8", f"Task file is not valid UTF-8: {task_path}", str(task_path)))
                continue
            except OSError as exc:
                errors.append(_error("task.read_failed", f"Cannot read task file {task_path}: {exc}", str(task_path)))
                continue
            try:
                frontmatter, body = _split_frontmatter(content)
            except ValueError as exc:
                errors.append(_error("task.invalid_frontmatter", str(exc), str(task_path)))
                continue

            changed = False
            task_id = frontmatter.get("id")
            if isinstance(task_id, int):
                frontmatter["id"] = str(task_id)
                changed = True
            elif not isinstance(task_id, str) or not task_id.strip():
                if fix:
                    while next_numeric_id in used_numeric_ids:
                        next_numeric_id += 1
                    frontmatter["id"] = str(next_numeric_id)
                    used_numeric_ids.add(next_numeric_id)
                    next_numeric_id += 1
                    changed = True
                else:
                    errors.append(_error(
                        "task.id_missing",
                        "Task is missing frontmatter id. Run `tulid validate --fix` to assign the next numeric id.",
                        str(task_path),
                    ))

            task_type = frontmatter.get("type")
            if not isinstance(task_type, str) or not task_type.strip():
                if fix:
                    frontmatter["type"] = "ProductIdea"
                    changed = True
                else:
                    errors.append(_error(
                        "task.type_missing",
                        "Task is missing frontmatter type. Run `tulid validate --fix` to assign ProductIdea.",
                        str(task_path),
                    ))

            board_state = note_states.get(task_path.stem)
            frontmatter_state = frontmatter.get("state")
            if board_state is not None and str(frontmatter_state or "") != board_state:
                if fix:
                    frontmatter["state"] = board_state
                    changed = True
                else:
                    errors.append(_error(
                        "task.state_mismatch",
                        (
                            f"Task frontmatter state {str(frontmatter_state)!r} does not match "
                            f"board column state {board_state!r}. Run `tulid validate --fix` to apply the board state."
                        ),
                        str(task_path),
                    ))

            if changed:
                writes.append(_PreparedWrite(
                    path=task_path,
                    content=_serialize_task_document(frontmatter, body),
                ))

        if fix and writes:
            written = self._write_prepared(writes)
            if not written.accepted:
                errors.extend(written.errors)
        return tuple(errors)

    def _load(self) -> _LoadedProject:
        loaded = _LoadedProject()
        project_root = self.config.project_root
        if not project_root.is_dir():
            loaded.errors.append(_error(
                "project.not_found",
                f"Project root does not exist: {project_root}",
                str(project_root),
            ))
            return loaded

        tasks_by_note: dict[str, _TaskDocument] = {}
        for task_path in sorted(self._tasks_root().glob("*.md")):
            doc = self._read_task_file(task_path)
            if isinstance(doc, DomainError):
                loaded.errors.append(doc)
                continue
            note_name = task_path.stem
            existing = loaded.task_paths_by_id.get(doc.task.id)
            if existing is not None:
                loaded.errors.append(_error(
                    "task.duplicate_id",
                    f"Task ID {doc.task.id} appears in both {existing} and {task_path}.",
                    doc.task.id,
                ))
                continue
            tasks_by_note[note_name] = doc
            loaded.task_paths_by_id[doc.task.id] = task_path
            loaded.task_notes_by_id[doc.task.id] = note_name

        board_positions: dict[str, BoardPosition] = {}
        for board_name, board_path_value in self.config.boards.items():
            board_path = self._project_path(board_path_value)
            board_doc = self._read_board(board_name, board_path)
            if isinstance(board_doc, DomainError):
                loaded.errors.append(board_doc)
                continue
            loaded.boards_by_name[board_name] = board_doc
            for card in board_doc.cards:
                task_doc = tasks_by_note.get(card.task_note)
                if task_doc is None:
                    loaded.errors.append(_error(
                        "task.link_missing",
                        f"Board card [[{card.task_note}]] does not resolve to a task file.",
                        f"{board_path}:{card.line_index + 1}",
                    ))
                    continue

                task_id = task_doc.task.id
                loaded.card_seen_task_ids.add(task_id)
                state = self._state_for_board_column(card.board, card.column)
                if state is None:
                    loaded.errors.append(_error(
                        "board.column_unmapped",
                        f"Board {card.board!r} column {card.column!r} is not mapped to a logical state.",
                        f"{board_path}:{card.line_index + 1}",
                    ))
                    continue

                existing_card = loaded.cards_by_task_id.get(task_id)
                if existing_card is not None:
                    loaded.errors.append(_error(
                        "task.duplicate_active_card",
                        f"Task {task_id} appears in more than one board card.",
                        task_id,
                    ))
                    continue

                loaded.cards_by_task_id[task_id] = card
                board_positions[task_id] = BoardPosition(
                    board=card.board,
                    column=card.column,
                    card_text=card.card_text,
                    line=card.line_index + 1,
                )

        for task_id in loaded.task_paths_by_id:
            if task_id not in loaded.card_seen_task_ids:
                loaded.errors.append(_error(
                    "task.missing_active_card",
                    f"Task {task_id} does not appear in any canonical board column.",
                    task_id,
                ))

        tasks: dict[str, Task] = {}
        for task_id, path in loaded.task_paths_by_id.items():
            note = loaded.task_notes_by_id[task_id]
            task_doc = tasks_by_note[note]
            state = self._state_for_task_id(task_id, loaded, task_doc.task.current_state)
            tasks[task_id] = Task(
                id=task_doc.task.id,
                title=task_doc.task.title,
                path=task_doc.task.path,
                current_state=state,
                task_type=task_doc.task.task_type,
                dependencies=task_doc.task.dependencies,
                artifact_links=task_doc.task.artifact_links,
                parent_id=task_doc.task.parent_id,
                metadata=task_doc.task.metadata,
                body=task_doc.task.body,
            )

        if not loaded.errors:
            loaded.snapshot = ProjectSnapshot(
                project_id=self.config.project_id,
                tasks=MappingProxyType(tasks),
                board_positions=MappingProxyType(board_positions),
            )
        return loaded

    def _read_task_file(self, path: Path) -> _TaskDocument | DomainError:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error("task.invalid_utf8", f"Task file is not valid UTF-8: {path}", str(path))
        except OSError as exc:
            return _error("task.read_failed", f"Cannot read task file {path}: {exc}", str(path))

        try:
            frontmatter, body = _split_frontmatter(content)
        except ValueError as exc:
            return _error("task.invalid_frontmatter", str(exc), str(path))
        task_id = frontmatter.get("id")
        if isinstance(task_id, int):
            task_id = str(task_id)
            frontmatter["id"] = task_id
        if not isinstance(task_id, str) or not task_id.strip():
            return _error("task.id_missing", "Task is missing frontmatter id.", str(path))
        task_id = task_id.strip()
        if not _is_valid_task_id(task_id):
            return _error("task.invalid_id", "Task ID must be a positive integer or legacy 26-character Crockford Base32 ULID.", str(path))

        title = _extract_title(body) or path.stem
        metadata = {k: v for k, v in frontmatter.items() if k not in {
            "id", "type", "state", "dependencies", "artifact_links", "parent_id",
        }}
        task = Task(
            id=task_id,
            title=title,
            path=self._domain_path(path),
            current_state=str(frontmatter.get("state", "")),
            task_type=str(frontmatter.get("type", "task")),
            dependencies=_string_tuple(frontmatter.get("dependencies")),
            artifact_links=_string_tuple(frontmatter.get("artifact_links")),
            parent_id=_optional_string(frontmatter.get("parent_id")),
            metadata=MappingProxyType(metadata),
            body=body,
        )
        return _TaskDocument(task=task, frontmatter=MappingProxyType(dict(frontmatter)))

    def _read_board(self, board: str, path: Path) -> _BoardDocument | DomainError:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return _error("board.not_found", f"Board file does not exist: {path}", str(path))
        except UnicodeDecodeError:
            return _error("board.invalid_utf8", f"Board file is not valid UTF-8: {path}", str(path))
        except OSError as exc:
            return _error("board.read_failed", f"Cannot read board file {path}: {exc}", str(path))

        cards: list[_BoardCard] = []
        insert_after_by_column: dict[str, int] = {}
        current_column: str | None = None
        in_frontmatter = False
        in_settings = False

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "---" and not in_settings:
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter:
                continue
            if stripped == "%% kanban:settings":
                in_settings = True
                continue
            if in_settings:
                if stripped == "%%":
                    in_settings = False
                continue
            if stripped.startswith("## "):
                current_column = stripped[3:].strip()
                insert_after_by_column[current_column] = idx + 1
                continue
            task_note = parse_task_row(line)
            if task_note is None:
                if stripped:
                    return _error(
                        "board.invalid_task_row",
                        "Task row must contain exactly one task link like [[Task 1]].",
                        f"{path}:{idx + 1}",
                    )
                continue
            if current_column is None:
                return _error(
                    "board.card_before_column",
                    "Task row appears before any section heading.",
                    f"{path}:{idx + 1}",
                )
            cards.append(_BoardCard(
                board=board,
                board_path=path,
                column=current_column,
                line_index=idx,
                card_text=line,
                task_note=task_note,
            ))
            insert_after_by_column[current_column] = idx + 1

        return _BoardDocument(
            board=board,
            path=path,
            lines=tuple(lines),
            cards=tuple(cards),
            insert_after_by_column=MappingProxyType(insert_after_by_column),
        )

    def _state_for_task_id(self, task_id: str, loaded: _LoadedProject, fallback: str) -> str:
        card = loaded.cards_by_task_id.get(task_id)
        if card is None:
            return fallback
        state = self._state_for_board_column(card.board, card.column)
        return state or fallback

    def _state_for_board_column(self, board: str, column: str) -> str | None:
        for mapping in self.config.state_mappings:
            if mapping.board == board and mapping.column == column:
                return mapping.state
        return None

    def _sync_task_state_cache(
        self,
        task_id: str,
        loaded: _LoadedProject,
        state: str,
        *,
        fallback_path: Path,
    ) -> WriteResult:
        task_path = loaded.task_paths_by_id.get(task_id)
        if task_path is None:
            return WriteResult(path=str(fallback_path))
        cache_update = self._prepare_task_state_cache(task_path, state)
        if cache_update.error is not None:
            return WriteResult(errors=(cache_update.error,))
        if cache_update.content is None:
            return WriteResult(path=str(fallback_path))
        return self._write_prepared((_PreparedWrite(path=task_path, content=cache_update.content),))

    def _mapping_for_state(self, state: str) -> ObsidianStateMapping | None:
        for mapping in self.config.state_mappings:
            if mapping.state == state:
                return mapping
        return None

    def _project_path(self, path: str | Path) -> Path:
        path_obj = Path(path)
        if path_obj.is_absolute():
            return path_obj
        return self.config.project_root / path_obj

    def _tasks_root(self) -> Path:
        return self._project_path(self.config.tasks_dir)

    def _events_root(self) -> Path:
        return self._project_path(self.config.events_dir)

    def _domain_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.config.project_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _write_text(self, path: Path, content: str) -> WriteResult:
        return self._write_prepared([_PreparedWrite(path=path, content=content)])

    def _write_prepared(self, writes: list[_PreparedWrite]) -> WriteResult:
        temp_paths: list[Path] = []
        originals: list[tuple[Path, bytes | None]] = []
        replaced: list[tuple[Path, bytes | None]] = []
        try:
            for write in writes:
                write.path.parent.mkdir(parents=True, exist_ok=True)
                originals.append((
                    write.path,
                    write.path.read_bytes() if write.path.exists() else None,
                ))
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{write.path.name}.",
                    suffix=".tmp",
                    dir=str(write.path.parent),
                    text=True,
                )
                temp_path = Path(tmp_name)
                temp_paths.append(temp_path)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(write.content)

            for write, temp_path, original in zip(writes, temp_paths, originals):
                os.replace(temp_path, write.path)
                replaced.append(original)
        except OSError as exc:
            for path, original in reversed(replaced):
                try:
                    if original is None:
                        if path.exists():
                            path.unlink()
                    else:
                        path.write_bytes(original)
                except OSError:
                    pass
            for temp_path in temp_paths:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass
            return WriteResult(errors=(_error(
                "file.write_failed",
                f"Cannot write files: {exc}",
                str(writes[-1].path) if writes else None,
            ),))
        return WriteResult(path=str(writes[-1].path) if writes else None)

    def _prepare_task_state_cache(self, path: Path, state: str) -> _TaskStateCacheUpdate:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _TaskStateCacheUpdate(error=_error(
                "task.invalid_utf8",
                f"Task file is not valid UTF-8: {path}",
                str(path),
            ))
        except OSError as exc:
            return _TaskStateCacheUpdate(error=_error(
                "task.read_failed",
                f"Cannot read task file {path}: {exc}",
                str(path),
            ))

        try:
            frontmatter, body = _split_frontmatter(content)
        except ValueError as exc:
            return _TaskStateCacheUpdate(error=_error(
                "task.invalid_frontmatter",
                str(exc),
                str(path),
            ))

        if "state" not in frontmatter:
            return _TaskStateCacheUpdate()

        frontmatter["state"] = state
        new_content = _serialize_frontmatter(frontmatter)
        if body:
            new_content += "\n\n" + body.strip("\n") + "\n"
        else:
            new_content += "\n"
        return _TaskStateCacheUpdate(content=new_content)


def config_from_workflow(
    *,
    project_id: str,
    project_root: Path,
    workflow: WorkflowDefinition,
    tasks_dir: str | Path = "tasks",
    events_dir: str | Path = "events",
) -> ObsidianAdapterConfig:
    return config_from_storage_definition(
        project_id=project_id,
        project_root=project_root,
        storage=workflow.storage,
        tasks_dir=tasks_dir,
        events_dir=events_dir,
    )


def config_from_storage_definition(
    *,
    project_id: str,
    project_root: Path,
    storage: StorageDefinition | None,
    tasks_dir: str | Path = "tasks",
    events_dir: str | Path = "events",
) -> ObsidianAdapterConfig:
    if storage is None:
        raise ValueError("workflow.storage is required for the configured storage adapter")
    boards_value = storage.config.get("boards")
    state_mappings_value = storage.config.get("state_mappings")
    if not isinstance(boards_value, Mapping):
        raise ValueError("workflow.storage.config.boards must be a mapping")
    if not isinstance(state_mappings_value, tuple):
        raise ValueError("workflow.storage.config.state_mappings must be a tuple")
    return ObsidianAdapterConfig(
        project_id=project_id,
        project_root=project_root,
        boards={str(name): str(path) for name, path in boards_value.items()},
        state_mappings=tuple(
            ObsidianStateMapping(
                state=str(mapping["state"]),
                board=str(mapping["board"]),
                column=str(mapping["column"]),
            )
            for mapping in state_mappings_value
        ),
        tasks_dir=tasks_dir,
        events_dir=events_dir,
    )


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            frontmatter = _parse_frontmatter("\n".join(lines[1:idx]))
            body = "\n".join(lines[idx + 1 :]).strip("\n")
            return frontmatter, body
    return {}, content


def _parse_frontmatter(source: str) -> dict[str, Any]:
    try:
        parsed = _yaml().load(source) if source.strip() else {}
    except Exception as exc:
        raise ValueError(f"Task frontmatter is not valid YAML: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return dict(parsed)


def _serialize_frontmatter(values: Mapping[str, Any]) -> str:
    output = StringIO()
    _yaml().dump({key: values[key] for key in sorted(values)}, output)
    return "---\n" + output.getvalue().rstrip("\n") + "\n---"


def _serialize_task(task: Task) -> str:
    frontmatter = dict(task.metadata)
    frontmatter["id"] = task.id
    frontmatter["type"] = task.task_type
    if task.current_state:
        frontmatter["state"] = task.current_state
    if task.dependencies:
        frontmatter["dependencies"] = list(task.dependencies)
    if task.artifact_links:
        frontmatter["artifact_links"] = list(task.artifact_links)
    if task.parent_id is not None:
        frontmatter["parent_id"] = task.parent_id
    title = task.title.strip()
    body = _strip_leading_title(task.body).strip("\n")
    content_lines = [_serialize_frontmatter(frontmatter), ""]
    if title:
        content_lines.extend([f"# {title}", ""])
    if body:
        content_lines.append(body)
        content_lines.append("")
    return "\n".join(content_lines)


def _serialize_task_document(frontmatter: Mapping[str, Any], body: str) -> str:
    content = _serialize_frontmatter(frontmatter)
    clean_body = body.strip("\n")
    if clean_body:
        content += "\n\n" + clean_body + "\n"
    else:
        content += "\n"
    return content


def _strip_leading_title(body: str) -> str:
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines = lines[1:]
    if not lines:
        return body
    first = lines[0].strip()
    if not first.startswith("# "):
        return body
    remaining = lines[1:]
    if remaining and not remaining[0].strip():
        remaining = remaining[1:]
    return "\n".join(remaining)


def _extract_title(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value if str(v).strip())
    return (str(value),)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None


def _is_valid_task_id(value: str) -> bool:
    return NUMERIC_TASK_ID_RE.match(value) is not None or ULID_RE.match(value) is not None


def _next_numeric_task_id(tasks_root: Path) -> str:
    return str(max(_existing_numeric_task_ids(tasks_root), default=0) + 1)


def _existing_numeric_task_ids(tasks_root: Path) -> set[int]:
    ids: set[int] = set()
    for path in tasks_root.glob("*.md"):
        try:
            frontmatter, _body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        task_id = frontmatter.get("id")
        if isinstance(task_id, int):
            ids.add(task_id)
            continue
        if isinstance(task_id, str) and NUMERIC_TASK_ID_RE.match(task_id.strip()) is not None:
            ids.add(int(task_id.strip()))
    return ids


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    return slug or "task"


def _event_date(event: Mapping[str, Any]) -> str:
    timestamp = event.get("timestamp")
    if isinstance(timestamp, str) and len(timestamp) >= 10:
        return timestamp[:10]
    return datetime.now(UTC).date().isoformat()


def _find_column_insert_index(lines: list[str], column: str) -> int | None:
    in_target = False
    insert_at: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_target:
                return insert_at if insert_at is not None else idx
            in_target = stripped[3:].strip() == column
            if in_target:
                insert_at = idx + 1
            continue
        if in_target and parse_task_row(line) is not None:
            insert_at = idx + 1
    if in_target:
        return insert_at if insert_at is not None else len(lines)
    return None
