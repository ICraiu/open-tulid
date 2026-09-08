from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from open_tulid.domain import DomainError, Task, TransitionDefinition


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
    is_execution_contract: bool = False
    is_canonical_question_round_answers: bool = False
    is_current_question_round_answers: bool = False


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
        canonical_answer_links = _canonical_question_round_answer_links(task)
        if len(canonical_answer_links) > 1:
            return ContextPacketResult(errors=(DomainError(
                "context.question_round_answer_conflict",
                "QuestionRound has multiple linked QuestionRoundFile artifacts; retain exactly one current answer source before review.",
                task.id,
            ),))
        canonical_answer_link = canonical_answer_links[0] if canonical_answer_links else None
        ancestor_answer_links = _ancestor_question_round_answer_links(parent_tasks)
        canonical_answer_refs = {
            _clean_ref(link)
            for link in (*ancestor_answer_links, *canonical_answer_links)
        }

        # Answer records are intentionally first and chronological.  A review of a
        # derived QuestionRound must see the complete explicit-answer history, not
        # only the file linked to its immediate parent/current card.
        queue: list[tuple[str, int, bool]] = []
        queue.extend((link, 0, True) for link in ancestor_answer_links)
        queue.extend((link, 0, True) for link in canonical_answer_links)
        queue.extend(
            (link, 0, True)
            for link in _active_artifact_links(task.artifact_links)
            if not _is_question_round_file_link(link)
        )
        queue.extend((link, 0, False) for link in _wiki_links(sanitize_task_body_for_runtime(task.body)))
        for parent_task in parent_tasks:
            queue.extend(
                (link, 0, True)
                for link in _active_artifact_links(parent_task.artifact_links)
                if not _is_implementation_task_file_link(link)
                and not _is_question_round_file_link(link)
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
                is_execution_contract=_is_implementation_contract_link(ref),
                is_canonical_question_round_answers=_clean_ref(ref) in canonical_answer_refs,
                is_current_question_round_answers=_clean_ref(ref) == _clean_ref(canonical_answer_link or ""),
            ))
            queue.extend((link, depth + 1, False) for link in _wiki_links(content))

        if errors:
            return ContextPacketResult(errors=tuple(errors))
        text = "\n\n".join(render_context_document(doc) for doc in docs)
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


def _is_implementation_contract_link(ref: str) -> bool:
    clean = Path(_clean_ref(ref))
    return (
        "ImplementationContract" in clean.parts
        or clean.name == "implementation-contract.yaml"
    )


def _is_question_round_file_link(ref: str) -> bool:
    return "QuestionRoundFile" in Path(_clean_ref(ref)).parts


def _canonical_question_round_answer_links(task: Task) -> tuple[str, ...]:
    """A QuestionRound's own QuestionRoundFile is its sole current answer record."""
    if task.task_type != "QuestionRound":
        return ()
    return tuple(link for link in task.artifact_links if _is_question_round_file_link(link))


def _ancestor_question_round_answer_links(parent_tasks: tuple[Task, ...]) -> tuple[str, ...]:
    """Return one canonical answer record from each ancestor round, oldest first.

    The current round retains its strict conflict check above.  Historical records
    are already persisted canonical answers; duplicate paths/content are removed by
    the normal resolver de-duplication pass.
    """
    links: list[str] = []
    for parent in parent_tasks:
        if parent.task_type != "QuestionRound":
            continue
        round_links = _canonical_question_round_answer_links(parent)
        if round_links:
            links.append(round_links[0])
    return tuple(links)


def _active_artifact_links(links: tuple[str, ...]) -> tuple[str, ...]:
    latest_contract_index = next(
        (
            index
            for index in range(len(links) - 1, -1, -1)
            if _is_implementation_contract_link(links[index])
        ),
        None,
    )
    return tuple(
        link
        for index, link in enumerate(links)
        if not _is_implementation_contract_link(link)
        or index == latest_contract_index
    )


def render_context_document(document: ContextDocument) -> str:
    """Render one resolved context document with its role label and policy.

    Shared by the legacy render path and the frozen contract path so both
    routes describe the same canonical answer precedence and reference policy.
    """
    return _render_context_document(document)


def _render_context_document(document: ContextDocument) -> str:
    if document.is_canonical_question_round_answers:
        label = "Canonical QuestionRound Answers" if document.is_current_question_round_answers else "Canonical QuestionRound Answer History"
        heading = f"# {label}: {document.ref}"
        policy = (
            "This is the sole authoritative answer record for its QuestionRound. "
            "The task body is the generated question template and is not an answer source. "
            "Treat text following a supported response label in this record as the user's "
            "authoritative answer regardless of label formatting. Supported labels are "
            "`Your answer:`, `Answer:`, and `Response:`, case-insensitively, with optional "
            "Markdown emphasis around the label (for example, `**Answer:**`). "
            "Later explicit answers override earlier conflicting answers. Never re-ask a "
            "settled question; create a follow-up only for a genuinely new blocking question "
            "that no earlier answer record resolves. "
            + (
                "Do not merge it with another version; multiple directly linked QuestionRoundFile "
                "artifacts are rejected before execution."
                if document.is_current_question_round_answers else
                "This historical record remains binding unless a later explicit answer conflicts with it."
            )
        )
    elif document.is_execution_contract:
        heading = f"# Generated Execution Contract: {document.ref}"
        policy = (
            "This validated contract is binding for implementation scope, interfaces, "
            "requirements, and checks. It refines the user-requested outcome but must "
            "never broaden or replace it."
        )
    else:
        heading = f"# Linked Reference Context: {document.ref}"
        policy = (
            "This document is background reference material. It supports implementation "
            "decisions but does not redefine the assigned task scope."
        )
    return (
        f"{heading}\n"
        f"{policy}\n"
        f"Source: {document.path}\n"
        f"SHA256: {document.sha256}\n\n"
        f"{document.content.strip()}"
    )


def load_parent_tasks(adapter, task: Task) -> tuple[Task, ...]:
    """Load the task's ancestor lineage, original idea first.

    Shared by the legacy render path and the frozen contract path so both
    resolve the same parent lineage and canonical answer history at job creation.
    """
    parents: list[Task] = []
    seen = {task.id}
    parent_id = task.parent_id
    while parent_id and parent_id not in seen and len(parents) < 64:
        seen.add(parent_id)
        loaded = adapter.read_task(parent_id)
        if not loaded.accepted or loaded.task is None:
            break
        parent = loaded.task
        parents.append(parent)
        parent_id = parent.parent_id
    return tuple(reversed(parents))


def task_for_context(task: Task, transition: TransitionDefinition) -> Task:
    """Exclude artifacts the transition itself is about to require or derive.

    A worker must produce the required artifact, not read the transition's own
    deliverable as already-settled reference context. Shared by both prompt routes.
    """
    excluded_artifact_types = set(transition.requires.artifacts)
    if transition.derives is not None and transition.derives.task_type != task.task_type:
        excluded_artifact_types.add(transition.derives.artifact_type)
    artifact_links = tuple(
        link for link in task.artifact_links
        if _artifact_type_from_link(link) not in excluded_artifact_types
    )
    return Task(
        id=task.id,
        title=task.title,
        path=task.path,
        current_state=task.current_state,
        task_type=task.task_type,
        dependencies=task.dependencies,
        artifact_links=artifact_links,
        parent_id=task.parent_id,
        metadata=task.metadata,
        body=task.body,
    )


def _artifact_type_from_link(link: str) -> str | None:
    parts = Path(link).parts
    try:
        index = parts.index("artifacts")
    except ValueError:
        return None
    if index + 2 >= len(parts):
        return None
    return parts[index + 2]


def sanitize_task_body_for_runtime(text: str) -> str:
    return DERIVED_TASKS_SECTION_RE.sub("", text.rstrip()).rstrip()
