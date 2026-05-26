from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from open_tulid.domain import DomainError, Task


WIKI_LINK_RE = re.compile(r"\[\[([^\]|#/\\]+(?:/[^\]|#\\]+)*)\]\]")
DERIVED_TASKS_SECTION_RE = re.compile(
    r"\n## Derived tasks\n(?:- \[\[[^\n]+\]\]\n?)+(?:\n## Derived tasks\n(?:- \[\[[^\n]+\]\]\n?)+)*\s*\Z",
)


@dataclass(frozen=True)
class ContextDocument:
    ref: str
    path: Path
    content: str
    sha256: str


@dataclass(frozen=True)
class ContextPacket:
    documents: tuple[ContextDocument, ...]
    text: str
    sha256: str


@dataclass(frozen=True)
class ContextPacketResult:
    packet: ContextPacket | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


class LinkedContextResolver:
    def __init__(self, project_root: Path, *, max_depth: int = 8, max_documents: int = 64, max_bytes: int = 512_000):
        self.project_root = project_root.resolve()
        self.max_depth = max_depth
        self.max_documents = max_documents
        self.max_bytes = max_bytes

    def build_context_packet(self, task: Task, *, parent_tasks: tuple[Task, ...] = ()) -> ContextPacketResult:
        docs: list[ContextDocument] = []
        errors: list[DomainError] = []
        seen: set[Path] = set()
        seen_hashes: set[str] = set()
        total_bytes = 0

        queue: list[tuple[str, int, bool]] = []
        queue.extend((link, 0, True) for link in task.artifact_links)
        queue.extend((link, 0, False) for link in _wiki_links(sanitize_task_body_for_runtime(task.body)))
        for parent_task in parent_tasks:
            queue.extend(
                (link, 0, True)
                for link in parent_task.artifact_links
                if not _is_implementation_task_file_link(link)
            )
            queue.extend((link, 0, False) for link in _wiki_links(sanitize_task_body_for_runtime(parent_task.body)))
        while queue:
            ref, depth, required = queue.pop(0)
            if _is_implementation_task_file_link(ref):
                continue
            if depth > self.max_depth:
                continue
            path = self._resolve(ref)
            if path is None:
                if required:
                    errors.append(DomainError(
                        "context.link_not_found",
                        f"Linked context file was not found: {ref}",
                        ref,
                    ))
                continue
            if path in seen:
                continue
            if len(docs) >= self.max_documents:
                errors.append(DomainError(
                    "context.document_limit",
                    f"Linked context exceeds document limit of {self.max_documents}.",
                    ref,
                ))
                break
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(DomainError(
                    "context.read_failed",
                    f"Cannot read linked context file {ref}: {exc}",
                    ref,
                ))
                continue
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content_hash in seen_hashes:
                seen.add(path)
                continue
            content_size = len(content.encode("utf-8"))
            if total_bytes + content_size > self.max_bytes:
                errors.append(DomainError(
                    "context.byte_limit",
                    f"Linked context exceeds byte limit of {self.max_bytes}.",
                    ref,
                ))
                break
            seen.add(path)
            seen_hashes.add(content_hash)
            total_bytes += content_size
            docs.append(ContextDocument(
                ref=ref,
                path=path,
                content=content,
                sha256=content_hash,
            ))
            queue.extend((link, depth + 1, False) for link in _wiki_links(content))

        if errors:
            return ContextPacketResult(errors=tuple(errors))
        text = "\n\n".join(
            f"# Linked Context: {doc.ref}\n"
            f"Source: {doc.path}\n"
            f"SHA256: {doc.sha256}\n\n"
            f"{doc.content.strip()}"
            for doc in docs
        )
        return ContextPacketResult(packet=ContextPacket(
            documents=tuple(docs),
            text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))

    def _resolve(self, ref: str) -> Path | None:
        clean = _clean_ref(ref)
        if not clean:
            return None
        raw = Path(clean)
        if raw.is_absolute() or ".." in raw.parts:
            return None
        candidates = (
            self.project_root / raw,
            self.project_root / "tasks" / f"{clean}.md",
            self.project_root / "docs" / f"{clean}.md",
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            if self.project_root != resolved and self.project_root not in resolved.parents:
                continue
            if resolved.is_file():
                return resolved
        return None


def _clean_ref(ref: str) -> str:
    text = ref.strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    return text.split("|", 1)[0].split("#", 1)[0].strip()


def _wiki_links(text: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in WIKI_LINK_RE.finditer(text))


def _is_implementation_task_file_link(ref: str) -> bool:
    return "ImplementationTaskFile" in Path(_clean_ref(ref)).parts


def sanitize_task_body_for_runtime(text: str) -> str:
    return DERIVED_TASKS_SECTION_RE.sub("", text.rstrip()).rstrip()
