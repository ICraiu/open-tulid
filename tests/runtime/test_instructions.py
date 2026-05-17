from pathlib import Path
from types import MappingProxyType

from open_tulid.domain import (
    RequirementDefinition,
    TaskTypeDefinition,
    TransitionDefinition,
    WorkerDefinition,
)
from open_tulid.runtime import AgentInstructionResolver


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
