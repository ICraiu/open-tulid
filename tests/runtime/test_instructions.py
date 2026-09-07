from pathlib import Path
from types import MappingProxyType

from open_tulid.domain import (
    RequirementDefinition,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkerDefinition,
)
from open_tulid.runtime import AgentInstructionResolver
from open_tulid.runtime.instructions import CANONICAL_QUESTION_ROUND_ANSWER_POLICY_MARKER


def test_prompt_packet_includes_default_worker_task_type_and_transition_layers(tmp_path: Path):
    agents = tmp_path / "agents"
    agents.mkdir()
    for name in ("default", "worker", "task", "transition"):
        (agents / f"{name}.agent.md").write_text(f"{name} instructions\n", encoding="utf-8")

    result = AgentInstructionResolver(tmp_path).build_prompt_packet(
        worker=WorkerDefinition(id="codex", instructions=("worker",)),
        task_type=TaskTypeDefinition(
            id="BackendTask",
            requirements_by_state=MappingProxyType({}),
            instructions=("task",),
        ),
        transition=TransitionDefinition(
            id="Implement",
            task_type="BackendTask",
            from_state="Todo",
            to_state="Review",
            worker="codex",
            requires=RequirementDefinition(),
            transaction=None,
            instructions=("transition",),
        ),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert [doc.ref for doc in result.packet.instructions] == [
        "default",
        "worker",
        "task",
        "transition",
    ]


def test_legacy_answers_review_is_rejected_without_overwriting_project_customization(tmp_path: Path):
    agents = tmp_path / "agents"
    agents.mkdir()
    legacy = "Treat text beneath each `Your answer:` label as the user's answer.\n"
    (agents / "answers-review.agent.md").write_text(legacy, encoding="utf-8")

    result = AgentInstructionResolver(tmp_path).build_prompt_packet(
        worker=WorkerDefinition(id="codex_clarity", instructions=("answers-review",)),
        task_type=TaskTypeDefinition(
            id="QuestionRound",
            requirements_by_state=MappingProxyType({}),
        ),
        transition=TransitionDefinition(
            id="ReviewAnswers",
            task_type="QuestionRound",
            from_state="AnswersReady",
            to_state="ReadyForSpec",
            worker="codex_clarity",
            requires=RequirementDefinition(),
            transaction=None,
        ),
    )

    assert result.accepted is False
    assert result.errors[0].code == "instructions.stale_canonical_question_round_policy"
    assert CANONICAL_QUESTION_ROUND_ANSWER_POLICY_MARKER in result.errors[0].message
    assert (agents / "answers-review.agent.md").read_text(encoding="utf-8") == legacy


def test_upgraded_answers_review_receives_binding_runtime_canonical_answer_policy(tmp_path: Path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "answers-review.agent.md").write_text(
        f"{CANONICAL_QUESTION_ROUND_ANSWER_POLICY_MARKER}\nLocal customization.\n",
        encoding="utf-8",
    )

    result = AgentInstructionResolver(tmp_path).build_prompt_packet(
        worker=WorkerDefinition(id="codex_clarity", instructions=("answers-review",)),
        task_type=TaskTypeDefinition(
            id="QuestionRound",
            requirements_by_state=MappingProxyType({}),
        ),
        transition=TransitionDefinition(
            id="ReviewAnswers",
            task_type="QuestionRound",
            from_state="AnswersReady",
            to_state="ReadyForSpec",
            worker="codex_clarity",
            requires=RequirementDefinition(),
            transaction=None,
        ),
    )

    assert result.accepted is True
    assert result.packet is not None
    assert result.packet.instructions[-1].ref == "runtime/canonical-question-round-answers-v1"
    assert "takes precedence over conflicting project-local agent wording" in result.packet.text
