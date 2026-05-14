from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from open_tulid.domain import DomainError, TransitionDefinition, WorkerDefinition


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
        transition: TransitionDefinition,
    ) -> PromptPacketResult:
        refs = _unique_refs((
            "default",
            *(worker.instructions if worker is not None else ()),
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


def _unique_refs(refs: tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref not in seen:
            ordered.append(ref)
            seen.add(ref)
    return tuple(ordered)
