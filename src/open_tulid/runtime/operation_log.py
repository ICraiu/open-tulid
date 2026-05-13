from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from open_tulid.domain import EventActor, EventType

from .events import JsonlEventStore, build_event, new_ulid


@dataclass(frozen=True)
class OperationEventLogger:
    store: JsonlEventStore

    def operation_started(
        self,
        context: Any,
        operation: str,
        args: Mapping[str, Any],
    ) -> None:
        self.store.append(build_event(
            project_id=_project_id(context),
            actor=EventActor(type=context.actor_type, id=context.actor_id),
            event_type=EventType.OperationStarted,
            correlation_id=context.correlation_id or new_ulid(),
            task_id=_task_id(context, args),
            transition_id=context.transition_id or _string(args.get("transition_id")),
            data={
                "operation": operation,
                "args": _json_safe(args),
            },
        ))

    def operation_finished(
        self,
        context: Any,
        operation: str,
        args: Mapping[str, Any],
        result: Any,
    ) -> None:
        self.store.append(build_event(
            project_id=_project_id(context),
            actor=EventActor(type=context.actor_type, id=context.actor_id),
            event_type=(
                EventType.OperationFinished
                if bool(getattr(result, "accepted", False))
                else EventType.OperationFailed
            ),
            correlation_id=context.correlation_id or new_ulid(),
            task_id=_task_id(context, args),
            transition_id=context.transition_id or _string(args.get("transition_id")),
            data={
                "operation": operation,
                "accepted": bool(getattr(result, "accepted", False)),
                "code": str(getattr(result, "code", "")),
                "message": str(getattr(result, "message", "")),
                "result": _json_safe(getattr(result, "data", {})),
                "errors": [
                    {"code": err.code, "message": err.message, "location": err.location}
                    for err in getattr(result, "errors", ())
                ],
            },
        ))


def _project_id(context: Any) -> str:
    if getattr(context, "project_id", None):
        return context.project_id
    if getattr(context, "snapshot", None) is not None:
        return context.snapshot.project_id
    return "unknown"


def _task_id(context: Any, args: Mapping[str, Any]) -> str | None:
    if getattr(context, "task", None) is not None:
        return context.task.id
    return _string(args.get("task_id"))


def _string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return repr(value)
