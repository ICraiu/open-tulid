from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from open_tulid.domain import DomainError, TaskTypeDefinition, TransitionDefinition, WorkerDefinition


CANONICAL_QUESTION_ROUND_ANSWER_POLICY_VERSION = "v1"
CANONICAL_QUESTION_ROUND_ANSWER_POLICY_MARKER = (
    "<!-- open-tulid:canonical-question-round-answers=v1 -->"
)
CANONICAL_QUESTION_ROUND_ANSWER_POLICY = """# Binding runtime policy: Canonical QuestionRound answers (v1)

This policy is binding and takes precedence over conflicting project-local agent wording. When context marks a `QuestionRoundFile` as `Canonical QuestionRound Answers` or `Canonical QuestionRound Answer History`, that file is the sole authoritative answer record for its round. The current task body is a generated question template, not an answer source. In the canonical record, text following `Your answer:`, `Answer:`, or `Response:` is a user answer; labels are case-insensitive and may use Markdown emphasis (for example, `**Answer:**`). Reconcile every supplied canonical answer record in chronological order; a later explicit answer overrides an earlier conflict. Given an explicit decision to proceed, do not create another question round that repeats a settled question. A follow-up may contain only a genuinely new blocking question that no earlier answer record resolves. Do not infer that answers are blank from the generated task body, and do not merge a current canonical record with another directly linked QuestionRoundFile.\n"""


@dataclass(frozen=True)
class InstructionDocument:
    ref: str
    path: Path
    content: str
    sha256: str


@dataclass(frozen=True)
class PromptPacket:
    instructions: tuple[InstructionDocument, ...]
    text: str
    sha256: str


@dataclass(frozen=True)
class PromptPacketResult:
    packet: PromptPacket | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


class AgentInstructionResolver:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.instructions_root = project_root / "agents"

    def build_prompt_packet(
        self,
        *,
        worker: WorkerDefinition | None,
        task_type: TaskTypeDefinition | None,
        transition: TransitionDefinition,
    ) -> PromptPacketResult:
        refs = _unique_refs((
            "default",
            *(worker.instructions if worker is not None else ()),
            *(task_type.instructions if task_type is not None else ()),
            *transition.instructions,
        ))
        docs: list[InstructionDocument] = []
        errors: list[DomainError] = []
        for ref in refs:
            resolved = self._resolve(ref)
            if isinstance(resolved, DomainError):
                if ref == "default" and resolved.code == "instructions.not_found":
                    continue
                errors.append(resolved)
                continue
            docs.append(resolved)
        if _requires_canonical_question_round_answer_policy(task_type, transition):
            answer_review = next((
                doc for doc in docs if doc.path.name == "answers-review.agent.md"
            ), None)
            if answer_review is not None and CANONICAL_QUESTION_ROUND_ANSWER_POLICY_MARKER not in answer_review.content:
                errors.append(DomainError(
                    "instructions.stale_canonical_question_round_policy",
                    "agents/answers-review.agent.md predates canonical QuestionRound answer policy v1. "
                    "Tulid will not silently replace project-local instructions. Review and merge the "
                    "current policy, then add the marker "
                    f"{CANONICAL_QUESTION_ROUND_ANSWER_POLICY_MARKER} to acknowledge the upgrade. "
                    "The runtime policy is applied after project instructions and is binding.",
                    str(answer_review.path),
                ))
            docs.append(_canonical_question_round_answer_policy_document())
        if errors:
            return PromptPacketResult(errors=tuple(errors))
        text = "\n\n".join(
            f"# Instruction: {doc.ref}\nSource: {doc.path}\nSHA256: {doc.sha256}\n\n{doc.content.strip()}"
            for doc in docs
        )
        return PromptPacketResult(packet=PromptPacket(
            instructions=tuple(docs),
            text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))

    def _resolve(self, ref: str) -> InstructionDocument | DomainError:
        if not ref.strip():
            return DomainError("instructions.invalid_ref", "Instruction reference is empty.")
        ref_path = Path(ref)
        if ref_path.is_absolute() or ".." in ref_path.parts:
            return DomainError(
                "instructions.invalid_ref",
                f"Instruction reference must stay inside agents/: {ref}",
                ref,
            )
        candidates = []
        if ref_path.suffix == ".md":
            candidates.append(self.instructions_root / ref_path)
        else:
            candidates.append(self.instructions_root / f"{ref}.agent.md")
            candidates.append(self.instructions_root / ref)
        matches = [path for path in candidates if path.is_file()]
        if len(matches) != 1:
            return DomainError(
                "instructions.not_found" if not matches else "instructions.ambiguous",
                f"Instruction reference must resolve to exactly one agents/*.agent.md file: {ref}",
                ref,
            )
        path = matches[0].resolve()
        root = self.instructions_root.resolve()
        if path != root and root not in path.parents:
            return DomainError(
                "instructions.invalid_ref",
                f"Instruction reference escapes agents/: {ref}",
                ref,
            )
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return DomainError(
                "instructions.read_failed",
                f"Cannot read instruction file: {exc}",
                str(path),
            )
        return InstructionDocument(
            ref=ref,
            path=path,
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )


def _requires_canonical_question_round_answer_policy(
    task_type: TaskTypeDefinition | None,
    transition: TransitionDefinition,
) -> bool:
    """Identify the standard answer-review gate without relying on a mutable prompt file."""
    return (
        task_type is not None
        and task_type.id == "QuestionRound"
        and transition.from_state == "AnswersReady"
    )


def _canonical_question_round_answer_policy_document() -> InstructionDocument:
    content = CANONICAL_QUESTION_ROUND_ANSWER_POLICY
    return InstructionDocument(
        ref=f"runtime/canonical-question-round-answers-{CANONICAL_QUESTION_ROUND_ANSWER_POLICY_VERSION}",
        path=Path("<open-tulid runtime policy>"),
        content=content,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _unique_refs(refs: tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref not in seen:
            ordered.append(ref)
            seen.add(ref)
    return tuple(ordered)
