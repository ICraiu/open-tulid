from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping


class FailureCategory(str, Enum):
    AUTHENTICATION = "authentication"
    PROVIDER = "provider"
    PERMISSION = "permission"
    CONTEXT = "context"
    TIMEOUT = "timeout"
    ENVIRONMENT = "environment"
    IMPLEMENTATION = "implementation"
    PROTOCOL = "protocol"


@dataclass(frozen=True)
class ExecutionFailure:
    """A small generic result classifying a worker's end state at the executor boundary.

    It carries a stable ``code``, a high-level ``category``, a short
    ``retry_action`` for bounded recovery, sanitized evidence text, and the
    persisted evidence path. The numeric worker return code is kept separately
    for compatibility and is never folded into ``code``.
    """

    code: str
    category: str
    retryable: bool
    retry_action: str
    evidence: str
    evidence_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "retry_action": self.retry_action,
            "evidence": self.evidence,
            "evidence_path": self.evidence_path,
        }


# An explicit worker-tool denial such as OpenCode's doom-loop permission stop.
# A bare "Unauthorized" string plus restrictive configuration is deliberately
# NOT sufficient: classification requires the corresponding event/signature.
_DOOM_LOOP_SIGNATURES = (
    re.compile(r"\bdoom[- ]loop\b", re.IGNORECASE),
    re.compile(r"\btool[ -]denial[ -]loop\b", re.IGNORECASE),
)

_TIMEOUT_SIGNATURES = (
    re.compile(r"\btimed out\b", re.IGNORECASE),
    re.compile(r"\btimeout\b", re.IGNORECASE),
)

_ENVIRONMENT_SIGNATURES = (
    re.compile(r"\bcommand not found\b", re.IGNORECASE),
    re.compile(r"\bno such file or directory\b", re.IGNORECASE),
    re.compile(r"\bcannot find (the )?module", re.IGNORECASE),
    re.compile(r"\bmodule '.*' .* not found\b", re.IGNORECASE),
)

_PERMISSION_SIGNATURES = (
    re.compile(r"\bpermission denied\b", re.IGNORECASE),
    re.compile(r"\boperation not permitted\b", re.IGNORECASE),
    re.compile(r"\baccess denied\b", re.IGNORECASE),
)

_CONTEXT_SIGNATURES = (
    re.compile(r"\bcontext length\b", re.IGNORECASE),
    re.compile(r"\bcontext window\b", re.IGNORECASE),
    re.compile(r"\bmaximum context\b", re.IGNORECASE),
    re.compile(r"\btoken limit\b", re.IGNORECASE),
    re.compile(r"\bcontext exhausted\b", re.IGNORECASE),
    re.compile(r"\bcontext overflow\b", re.IGNORECASE),
)

_REJECTION_REASON_TO_FAILURE = {
    "session_expired": ExecutionFailure(
        code="session.expired",
        category=FailureCategory.AUTHENTICATION.value,
        retryable=True,
        retry_action="retry with a correctly bounded managed credential",
        evidence="rejection reason=session_expired",
    ),
    "session_unknown": ExecutionFailure(
        code="session.unknown",
        category=FailureCategory.AUTHENTICATION.value,
        retryable=True,
        retry_action="retry with a fresh managed credential",
        evidence="rejection reason=session_unknown",
    ),
    "lost_lease": ExecutionFailure(
        code="lease.lost",
        category=FailureCategory.PERMISSION.value,
        retryable=False,
        retry_action="stop; do not relax resource permissions",
        evidence="rejection reason=lost_lease",
    ),
    "wrong_proxy": ExecutionFailure(
        code="proxy.wrong",
        category=FailureCategory.PROTOCOL.value,
        retryable=False,
        retry_action="stop; managed credential targets the wrong proxy",
        evidence="rejection reason=wrong_proxy",
    ),
    "unknown_proxy": ExecutionFailure(
        code="proxy.unknown",
        category=FailureCategory.ENVIRONMENT.value,
        retryable=False,
        retry_action="stop; missing proxy configuration",
        evidence="rejection reason=unknown_proxy",
    ),
}


def failure_from_metadata(metadata: Mapping[str, object]) -> ExecutionFailure | None:
    """Reconstruct a classification persisted on a job for bounded recovery.

    The executor persists the small generic result (``failure_code``,
    ``failure_category``, ``retryable``, ``retry_action``, sanitized evidence,
    and evidence path) before the scheduler decides a retry. This reads that
    record without re-scanning logs. A missing code means the failure was never
    classified — that unknown is left ambiguous and recovery is allowed rather
    than inventing a cause.
    """
    code = metadata.get("failure_code")
    if not isinstance(code, str) or not code:
        return None
    retryable = metadata.get("retryable")
    if isinstance(retryable, bool):
        retryable_value = retryable
    else:
        retryable_value = True
    evidence_path = metadata.get("failure_evidence_path")
    return ExecutionFailure(
        code=code,
        category=str(metadata.get("failure_category", "")),
        retryable=retryable_value,
        retry_action=str(metadata.get("retry_action", "")),
        evidence=str(metadata.get("failure_evidence", "")),
        evidence_path=str(evidence_path) if isinstance(evidence_path, str) and evidence_path else None,
    )


def classify_worker_failure(
    *,
    returncode: int | None,
    stdout: str = "",
    stderr: str = "",
    job_id: str | None = None,
    proxy_evidence_root: Path | None = None,
    tool_events_path: Path | None = None,
    evidence_path: str | None = None,
) -> ExecutionFailure | None:
    """Classify a worker failure without inventing a reason.

    The classifier reads structured proxy/adapter evidence first, then explicit
    worker-tool event/signature evidence, then sanitized log signatures. A bare
    unauthorized/authentication-like log string without structured evidence is
    ambiguous and is never promoted to a provider or authentication failure.
    """
    from_proxy = _from_proxy_evidence(proxy_evidence_root, job_id, evidence_path)
    if from_proxy is not None:
        return from_proxy

    combined = f"{stdout}\n{stderr}"
    tool = _from_tool_events(tool_events_path, combined, evidence_path)
    if tool is not None:
        return tool

    return _from_log_signatures(returncode, combined, evidence_path)


def _from_proxy_evidence(
    proxy_evidence_root: Path | None,
    job_id: str | None,
    evidence_path: str | None,
) -> ExecutionFailure | None:
    if proxy_evidence_root is None:
        return None
    rejection = _rejection_failure(proxy_evidence_root, job_id, evidence_path)
    if rejection is not None:
        return rejection
    provider = _provider_failure(proxy_evidence_root, job_id, evidence_path)
    if provider is not None:
        return provider
    return None


def _rejection_failure(
    proxy_evidence_root: Path,
    job_id: str | None,
    evidence_path: str | None,
) -> ExecutionFailure | None:
    path = proxy_evidence_root / "rejections.jsonl"
    if not path.is_file():
        return None
    for line in _read_jsonl(path):
        if job_id is not None and line.get("job_id") != job_id:
            continue
        reason = line.get("reason")
        base = _REJECTION_REASON_TO_FAILURE.get(str(reason))
        if base is None:
            continue
        return _with_path(base, _rejection_evidence(path, str(reason)), evidence_path=str(path))
    return None


def _provider_failure(
    proxy_evidence_root: Path,
    job_id: str | None,
    evidence_path: str | None,
) -> ExecutionFailure | None:
    if job_id is None:
        return None
    job_dir = proxy_evidence_root / job_id
    if not job_dir.is_dir():
        return None
    for path in sorted(job_dir.glob("*.jsonl")):
        for line in _read_jsonl(path):
            raw_status = line.get("status")
            if not isinstance(raw_status, int):
                continue
            if 500 <= raw_status <= 599 or raw_status == 429:
                method = line.get("method", "")
                target = line.get("path", "")
                return ExecutionFailure(
                    code="provider.upstream",
                    category=FailureCategory.PROVIDER.value,
                    retryable=True,
                    retry_action="retry within the total attempt budget honoring provider timing",
                    evidence=f"upstream status={raw_status} method={method} path={target}",
                    evidence_path=str(path),
                )
    return None


def _from_tool_events(
    tool_events_path: Path | None,
    combined: str,
    evidence_path: str | None,
) -> ExecutionFailure | None:
    text = _read_text(tool_events_path)
    scanned = f"{combined}\n{text}"
    for pattern in _DOOM_LOOP_SIGNATURES:
        match = pattern.search(scanned)
        if match is None:
            continue
        return ExecutionFailure(
            code="tool.doom_loop",
            category=FailureCategory.IMPLEMENTATION.value,
            retryable=False,
            retry_action="report the tool denial signal; do not auto-loop",
            evidence=_bounded_sanitized(_line_around(scanned, match), 200),
            evidence_path=evidence_path,
        )
    return None


def _from_log_signatures(
    returncode: int | None,
    combined: str,
    evidence_path: str | None,
) -> ExecutionFailure | None:
    if returncode == 124 or _matches(_TIMEOUT_SIGNATURES, combined) is not None:
        return ExecutionFailure(
            code="worker.timeout",
            category=FailureCategory.TIMEOUT.value,
            retryable=True,
            retry_action="retry within the total attempt budget",
            evidence=_bounded_sanitized(_matches(_TIMEOUT_SIGNATURES, combined) or "worker timed out", 200),
            evidence_path=evidence_path,
        )
    if returncode == 127:
        return ExecutionFailure(
            code="worker.environment",
            category=FailureCategory.ENVIRONMENT.value,
            retryable=False,
            retry_action="stop; report the missing tool/dependency blocker",
            evidence=_bounded_sanitized(_matches(_ENVIRONMENT_SIGNATURES, combined) or "missing tool or command", 200),
            evidence_path=evidence_path,
        )
    env_line = _matches(_ENVIRONMENT_SIGNATURES, combined)
    if env_line is not None:
        return ExecutionFailure(
            code="worker.environment",
            category=FailureCategory.ENVIRONMENT.value,
            retryable=False,
            retry_action="stop; report the missing tool/dependency blocker",
            evidence=_bounded_sanitized(env_line, 200),
            evidence_path=evidence_path,
        )
    permission_line = _matches(_PERMISSION_SIGNATURES, combined)
    if permission_line is not None:
        return ExecutionFailure(
            code="worker.permission",
            category=FailureCategory.PERMISSION.value,
            retryable=False,
            retry_action="stop; do not relax permissions automatically",
            evidence=_bounded_sanitized(permission_line, 200),
            evidence_path=evidence_path,
        )
    context_line = _matches(_CONTEXT_SIGNATURES, combined)
    if context_line is not None:
        return ExecutionFailure(
            code="worker.context",
            category=FailureCategory.CONTEXT.value,
            retryable=True,
            retry_action="retry with a valid reduced context packet",
            evidence=_bounded_sanitized(context_line, 200),
            evidence_path=evidence_path,
        )
    return None


def _with_path(
    base: ExecutionFailure,
    evidence: str,
    *,
    evidence_path: str | None = None,
) -> ExecutionFailure:
    return ExecutionFailure(
        code=base.code,
        category=base.category,
        retryable=base.retryable,
        retry_action=base.retry_action,
        evidence=evidence,
        evidence_path=evidence_path or base.evidence_path,
    )


def _rejection_evidence(path: Path, item: str) -> str:
    stem = path.name or "rejections.jsonl"
    return f"rejection reason={item} source={stem}"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    text = _read_text(path)
    records: list[dict[str, object]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, Mapping):
            records.append(dict(parsed))  # type: ignore[arg-type]
    return records


def _read_text(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            return _line_around(text, match)
    return None


def _line_around(text: str, match: re.Match[str]) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end == -1:
        end = len(text)
    return text[start:end].strip()


_TOKEN_REDACTIONS = (
    re.compile(r"Bearer [A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|session[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"[A-Za-z0-9_\-]{36,}"),
)


def _bounded_sanitized(text: str, limit: int) -> str:
    sanitized = text
    for pattern in _TOKEN_REDACTIONS:
        sanitized = pattern.sub("<redacted>", sanitized)
    if len(sanitized) <= limit:
        return sanitized
    return sanitized[:limit] + "…"


__all__ = [
    "ExecutionFailure",
    "FailureCategory",
    "classify_worker_failure",
    "failure_from_metadata",
]
