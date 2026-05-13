from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from open_tulid.domain import DomainError, EventEnvelope, TransactionJournalRecord

from .events import JsonlEventStore, TransactionJournalStore


class EffectResult(Protocol):
    accepted: bool
    message: str
    errors: tuple[DomainError, ...]


EffectApplier = Callable[[Mapping[str, object]], EffectResult]


@dataclass(frozen=True)
class TransactionApplyResult:
    accepted: bool
    journal: TransactionJournalRecord | None = None
    error: DomainError | None = None


class FileTransactionRuntime:
    def __init__(
        self,
        *,
        journals: TransactionJournalStore,
        events: JsonlEventStore,
        apply_effect: EffectApplier,
    ):
        self.journals = journals
        self.events = events
        self.apply_effect = apply_effect

    def apply(
        self,
        *,
        project_id: str,
        effects: tuple[Mapping[str, object], ...],
        events: tuple[EventEnvelope, ...],
        journal_id: str | None = None,
        task_id: str | None = None,
        transition_id: str | None = None,
    ) -> TransactionApplyResult:
        prepared = self.journals.prepare(
            journal_id=journal_id,
            project_id=project_id,
            task_id=task_id,
            transition_id=transition_id,
            effects=effects,
            events=events,
        )
        if not prepared.accepted:
            return TransactionApplyResult(
                accepted=False,
                error=prepared.error,
            )
        if prepared.record is None:
            return TransactionApplyResult(
                accepted=False,
                error=DomainError(
                    code="journal.prepare_failed",
                    message="Journal prepare returned no record.",
                ),
            )

        record = prepared.record
        for effect in effects:
            try:
                result = self.apply_effect(effect)
            except Exception as exc:
                error = DomainError(
                    code="effect.exception",
                    message=f"Effect raised exception: {exc}",
                )
                failed = self.journals.fail(record, error)
                return TransactionApplyResult(
                    accepted=False,
                    journal=failed.record or record,
                    error=error,
                )
            if not result.accepted:
                error = _operation_error(effect, result)
                failed = self.journals.fail(record, error)
                return TransactionApplyResult(
                    accepted=False,
                    journal=failed.record or record,
                    error=error,
                )

        append_result = self.events.append_many(events)
        if not append_result.accepted:
            error = append_result.error or DomainError(
                code="event.append_failed",
                message="Event append failed.",
            )
            failed = self.journals.fail(record, error)
            return TransactionApplyResult(
                accepted=False,
                journal=failed.record or record,
                error=error,
            )

        committed = self.journals.commit(record)
        if not committed.accepted:
            return TransactionApplyResult(
                accepted=False,
                journal=record,
                error=committed.error,
            )
        return TransactionApplyResult(
            accepted=True,
            journal=committed.record,
        )


def _operation_error(effect: Mapping[str, object], result: EffectResult) -> DomainError:
    if result.errors:
        return result.errors[0]
    return DomainError(
        code="effect.failed",
        message=result.message or f"Effect failed: {dict(effect)}",
    )
