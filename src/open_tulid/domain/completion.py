from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from open_tulid.domain.schema import WorkflowDefinition

SUCCESS = "success"
FAILURE = "failure"
CANCELLED = "cancelled"

TERMINAL_OUTCOMES: tuple[str, ...] = (SUCCESS, FAILURE, CANCELLED)

# Backward-compatible legacy outcome used when a terminal state has no explicit
# declaration (see ``dependency_outcome``); never reinterprets a declared
# failure/cancelled state as success.
LEGACY_AMBIGUOUS_OUTCOME = SUCCESS


@dataclass(frozen=True)
class SuccessfulCompletionCriteria:
    """The single shared predicate for a verified implementation success.

    Every acceptance path that claims a task is successfully implemented must
    satisfy all conjuncts:

    - ``acceptance_verified``: the required global checks were accepted for the
      exact candidate source bytes.
    - ``delivery_transaction_committed``: the delivery/commit transaction was
      durably committed for that candidate.
    - ``review_satisfied``: the required review evidence is present where the
      workflow configures review; vacuously satisfied when no review is
      configured.
    - ``final_state_consistent``: the final task/board state matches the
      transition's declared ``to_state``.

    Nothing is hardcoded to a state spelling, task type name, or worker
    identity; the configured workflow and transition supply which review
    evidence is required and which target state is expected.
    """

    acceptance_verified: bool
    delivery_transaction_committed: bool
    review_satisfied: bool
    final_state_consistent: bool

    def succeeded(self) -> bool:
        return all((
            self.acceptance_verified,
            self.delivery_transaction_committed,
            self.review_satisfied,
            self.final_state_consistent,
        ))

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, ok in (
                ("acceptance_verified", self.acceptance_verified),
                ("delivery_transaction_committed", self.delivery_transaction_committed),
                ("review_satisfied", self.review_satisfied),
                ("final_state_consistent", self.final_state_consistent),
            )
            if not ok
        )


def terminal_outcome_of(workflow: WorkflowDefinition, state_id: str) -> str | None:
    """Declared terminal outcome for a state, or ``None`` when not declared."""
    state = workflow.states.get(state_id)
    if state is None:
        return None
    return state.terminal_outcome


def is_terminal_state(workflow: WorkflowDefinition, state_id: str) -> bool:
    """Whether the state explicitly declares a terminal outcome."""
    return terminal_outcome_of(workflow, state_id) is not None


def outgoing_transitions_for(
    workflow: WorkflowDefinition,
    task_type: str,
    state_id: str,
) -> tuple:
    """Transitions leaving ``state_id`` for ``task_type``."""
    return tuple(
        transition
        for transition in workflow.transitions.values()
        if transition.task_type == task_type and transition.from_state == state_id
    )


def has_outgoing_transition(workflow, task_type: str, state_id: str) -> bool:
    return bool(outgoing_transitions_for(workflow, task_type, state_id))


def dependency_outcome(workflow, task_type: str, state_id: str) -> str:
    """Classify a dependency's terminal semantics for scheduling.

    - ``unmet``: the dependency still has an outgoing transition and is not
      finished.
    - ``failure`` / ``cancelled``: the dependency reached a declared
      non-success terminal state.
    - ``success``: the dependency reached a declared success terminal, or a
      legacy terminal state with no declaration (backward compatible during
      migration; never reinterprets a declared failure/cancelled state).
    """
    if has_outgoing_transition(workflow, task_type, state_id):
        return "unmet"
    outcome = terminal_outcome_of(workflow, state_id)
    if outcome is None:
        return LEGACY_AMBIGUOUS_OUTCOME
    return outcome


def ambiguous_terminal_states(workflow: WorkflowDefinition) -> tuple[str, ...]:
    """Terminal states (no outgoing transitions) without a declared outcome.

    These are the legacy states needing an explicit migration diagnostic.
    """
    outgoing = {transition.from_state for transition in workflow.transitions.values()}
    return tuple(
        state_id
        for state_id, state in workflow.states.items()
        if state_id not in outgoing and state.terminal_outcome is None
    )


def contradictory_terminal_states(workflow: WorkflowDefinition) -> tuple[str, ...]:
    """Declared terminal states that still have an outgoing transition.

    A supposedly terminal state with a contradictory outgoing transition is a
    workflow defect; dependents must never advance against it.
    """
    outgoing = {transition.from_state for transition in workflow.transitions.values()}
    return tuple(
        state_id
        for state_id, state in workflow.states.items()
        if state.terminal_outcome is not None and state_id in outgoing
    )
