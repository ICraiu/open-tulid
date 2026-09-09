from __future__ import annotations

from types import MappingProxyType

import pytest

from open_tulid.domain.completion import (
    LEGACY_AMBIGUOUS_OUTCOME,
    SuccessfulCompletionCriteria,
    ambiguous_terminal_states,
    contradictory_terminal_states,
    dependency_outcome,
    has_outgoing_transition,
    is_terminal_state,
    terminal_outcome_of,
)
from open_tulid.domain.schema import (
    RequirementDefinition,
    StateDefinition,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkflowDefinition,
)


def _transition(
    *,
    tid: str,
    task_type: str,
    from_state: str,
    to_state: str,
    worker: str,
) -> TransitionDefinition:
    return TransitionDefinition(
        id=tid,
        task_type=task_type,
        from_state=from_state,
        to_state=to_state,
        worker=worker,
        requires=RequirementDefinition(),
        transaction=None,
        default_for_scheduler=True,
    )


def _workflow(
    *,
    states,
    task_types,
    transitions,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        schema_version=1,
        states=MappingProxyType({sid: StateDefinition(id=sid, **kw) for sid, kw in states.items()}),
        task_types=MappingProxyType({
            ttid: TaskTypeDefinition(id=ttid, requirements_by_state=MappingProxyType({}))
            for ttid in task_types
        }),
        artifact_types=MappingProxyType({}),
        validation_types=MappingProxyType({}),
        operation_types=MappingProxyType({}),
        workers=MappingProxyType({}),
        transitions=MappingProxyType({
            tid: _transition(tid=tid, **trans_kw)
            for tid, trans_kw in transitions.items()
        }),
    )


def test_success_predicate_requires_all_four_conjuncts():
    ok = SuccessfulCompletionCriteria(
        acceptance_verified=True,
        delivery_transaction_committed=True,
        review_satisfied=True,
        final_state_consistent=True,
    )
    assert ok.succeeded() is True
    assert ok.gaps == ()

    for field in (
        "acceptance_verified",
        "delivery_transaction_committed",
        "review_satisfied",
        "final_state_consistent",
    ):
        kwargs = {
            "acceptance_verified": True,
            "delivery_transaction_committed": True,
            "review_satisfied": True,
            "final_state_consistent": True,
        }
        kwargs[field] = False
        assert SuccessfulCompletionCriteria(**kwargs).succeeded() is False
        assert field in SuccessfulCompletionCriteria(**kwargs).gaps


def test_success_predicate_is_not_hardcoded_to_state_or_worker_spelling():
    # Arbitrary state names, a custom task type, and an arbitrary worker id:
    # success must not depend on any literal spelling.
    ok = SuccessfulCompletionCriteria(
        acceptance_verified=True,
        delivery_transaction_committed=True,
        review_satisfied=True,
        final_state_consistent=True,
    )
    assert ok.succeeded() is True


def test_terminal_outcome_helpers_work_with_renamed_states():
    workflow = _workflow(
        states={
            "Inbox": {},
            "Shipped": {"terminal_outcome": "success"},
            "Rejected": {"terminal_outcome": "failure"},
        },
        task_types=("widget",),
        transitions={},
    )
    assert terminal_outcome_of(workflow, "Shipped") == "success"
    assert terminal_outcome_of(workflow, "Rejected") == "failure"
    assert terminal_outcome_of(workflow, "Inbox") is None
    assert is_terminal_state(workflow, "Shipped") is True
    assert is_terminal_state(workflow, "Inbox") is False


def test_no_outgoing_transition_is_required_for_terminal():
    workflow = _workflow(
        states={"Todo": {}, "Done": {"terminal_outcome": "success"}},
        task_types=("widget",),
        transitions={
            "go": {
                "task_type": "widget",
                "from_state": "Todo",
                "to_state": "Done",
                "worker": "worker-7",
            },
        },
    )
    assert has_outgoing_transition(workflow, "widget", "Todo") is True
    assert has_outgoing_transition(workflow, "widget", "Done") is False


def test_dependency_outcome_by_declared_terminal_outcome():
    workflow = _workflow(
        states={
            "Todo": {},
            "Done": {"terminal_outcome": "success"},
            "Cancelled": {"terminal_outcome": "cancelled"},
            "Failed": {"terminal_outcome": "failure"},
            "LegacyEnd": {},
        },
        task_types=("custom",),
        transitions={
            "go": {
                "task_type": "custom",
                "from_state": "Todo",
                "to_state": "Done",
                "worker": "custom-worker",
            },
        },
    )
    assert dependency_outcome(workflow, "custom", "Todo") == "unmet"
    assert dependency_outcome(workflow, "custom", "Done") == "success"
    assert dependency_outcome(workflow, "custom", "Cancelled") == "cancelled"
    assert dependency_outcome(workflow, "custom", "Failed") == "failure"
    # Legacy ambiguous terminal: backward compatible success, never reinvents a
    # failed/cancelled meaning.
    assert dependency_outcome(workflow, "custom", "LegacyEnd") == LEGACY_AMBIGUOUS_OUTCOME


def test_ambiguous_terminal_states_collects_undeclared_terminals():
    workflow = _workflow(
        states={
            "Todo": {},
            "Done": {"terminal_outcome": "success"},
            "LegacyEnd": {},
        },
        task_types=("task",),
        transitions={
            "go": {
                "task_type": "task",
                "from_state": "Todo",
                "to_state": "Done",
                "worker": "w",
            },
        },
    )
    assert ambiguous_terminal_states(workflow) == ("LegacyEnd",)


def test_contradictory_terminal_states_detects_terminal_with_outgoing_transition():
    workflow = _workflow(
        states={
            "Done": {"terminal_outcome": "success"},
        },
        task_types=("task",),
        transitions={
            "extra": {
                "task_type": "task",
                "from_state": "Done",
                "to_state": "Todo",
                "worker": "w",
            },
        },
    )
    assert contradictory_terminal_states(workflow) == ("Done",)


def test_artifact_only_planning_transitions_are_valid_without_code_dff():
    # A question-generation/planning task may be artifact-only: it has no
    # changed_files requirement, and the shared predicate does not force a code
    # diff or application-test report for such task types.
    workflow = _workflow(
        states={
            "Idea": {},
            "Ready": {"terminal_outcome": "success"},
        },
        task_types=("QuestionRound",),
        transitions={
            "draft": {
                "task_type": "QuestionRound",
                "from_state": "Idea",
                "to_state": "Ready",
                "worker": "clarity-worker",
            },
        },
    )
    assert dependency_outcome(workflow, "QuestionRound", "Ready") == "success"
    assert has_outgoing_transition(workflow, "QuestionRound", "Ready") is False


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_declared_terminal_outcome_is_spelling_independent(outcome):
    state_id = "FINAL_STATE"
    workflow = _workflow(
        states={state_id: {"terminal_outcome": outcome}},
        task_types=(),
        transitions={},
    )
    assert dependency_outcome(workflow, "anything", state_id) == outcome
