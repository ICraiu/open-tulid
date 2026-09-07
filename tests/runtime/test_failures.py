from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_tulid.runtime.failures import ExecutionFailure, FailureCategory, classify_worker_failure


def _write_rejection(root: Path, job_id: str, reason: str) -> Path:
    path = root / "rejections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"job_id": job_id, "reason": reason, "http_status": 401}) + "\n")
    return path


def test_classifies_expired_managed_credential_from_proxy_evidence(tmp_path: Path):
    rejection = _write_rejection(tmp_path, "job-1", "session_expired")
    failure = classify_worker_failure(
        returncode=1,
        stderr="Error: Unauthorized: unauthorized\n",
        job_id="job-1",
        proxy_evidence_root=tmp_path,
    )
    assert failure is not None
    assert failure.code == "session.expired"
    assert failure.category == FailureCategory.AUTHENTICATION.value
    assert failure.retryable is True
    assert "correctly bounded" in failure.retry_action
    assert failure.evidence_path == str(rejection)


def test_classifies_lost_lease_as_permission(tmp_path: Path):
    _write_rejection(tmp_path, "job-1", "lost_lease")
    failure = classify_worker_failure(
        returncode=1,
        job_id="job-1",
        proxy_evidence_root=tmp_path,
    )
    assert failure is not None
    assert failure.category == FailureCategory.PERMISSION.value
    assert failure.retryable is False
    assert "do not relax" in failure.retry_action


def test_classifies_upstream_provider_error_from_transcript(tmp_path: Path):
    job_dir = tmp_path / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "openai.jsonl").write_text(
        json.dumps({
            "proxy_id": "openai",
            "job_id": "job-1",
            "method": "POST",
            "path": "/chat/completions",
            "status": 503,
        }) + "\n",
        encoding="utf-8",
    )
    failure = classify_worker_failure(
        returncode=1,
        stdout="tried to generate\n",
        job_id="job-1",
        proxy_evidence_root=tmp_path,
    )
    assert failure is not None
    assert failure.code == "provider.upstream"
    assert failure.category == FailureCategory.PROVIDER.value
    assert failure.retryable is True
    assert "status=503" in failure.evidence


def test_classifies_explicit_doom_loop_signature(tmp_path: Path):
    failure = classify_worker_failure(
        returncode=1,
        stdout="the agent kept hitting a doom-loop denial\n",
    )
    assert failure is not None
    assert failure.code == "tool.doom_loop"
    assert failure.category == FailureCategory.IMPLEMENTATION.value
    assert failure.retryable is False


def test_bare_unauthorized_log_string_is_not_classified():
    """An authentication-like string must not become a provider failure by itself."""
    failure = classify_worker_failure(
        returncode=1,
        stdout="OpenCode edited/generated fixture hashes repeatedly.\n",
        stderr="Error: Unauthorized: unauthorized\n",
    )
    assert failure is None


def test_unauthorized_with_restrictive_config_is_not_doom_loop():
    """An unauthorized string plus restrictive config is insufficient a doom-loop."""
    failure = classify_worker_failure(
        returncode=1,
        stdout="agent config denies doom_loop\n",
        stderr="Error: Unauthorized: unauthorized\n",
    )
    assert failure is None


def test_classifies_worker_timeout():
    failure = classify_worker_failure(returncode=124, stdout="work in progress\n")
    assert failure is not None
    assert failure.category == FailureCategory.TIMEOUT.value
    assert failure.code == "worker.timeout"
    assert failure.retryable is True


def test_classifies_timeout_signature_from_log_text():
    failure = classify_worker_failure(returncode=1, stderr="the run timed out after 7200 seconds\n")
    assert failure is not None
    assert failure.category == FailureCategory.TIMEOUT.value


def test_classifies_missing_tool_as_environment():
    failure = classify_worker_failure(returncode=1, stderr="bash: npm: command not found\n")
    assert failure is not None
    assert failure.code == "worker.environment"
    assert failure.category == FailureCategory.ENVIRONMENT.value
    assert failure.retryable is False
    assert "missing tool" in failure.retry_action


def test_classifies_command_not_found_returncode():
    failure = classify_worker_failure(returncode=127, stderr="python: can't open file 'x'\n")
    assert failure is not None
    assert failure.category == FailureCategory.ENVIRONMENT.value


def test_classifies_permission_denied():
    failure = classify_worker_failure(returncode=1, stderr="mkdir: permission denied\n")
    assert failure is not None
    assert failure.code == "worker.permission"
    assert failure.category == FailureCategory.PERMISSION.value
    assert failure.retryable is False


def test_classifies_context_exhaustion():
    failure = classify_worker_failure(returncode=1, stdout="context length exceeded\n")
    assert failure is not None
    assert failure.code == "worker.context"
    assert failure.category == FailureCategory.CONTEXT.value
    assert failure.retryable is True
    assert "reduced context packet" in failure.retry_action


def test_sanitized_evidence_redacts_tokens():
    failure = classify_worker_failure(
        returncode=1,
        stderr="mkdir: permission denied api_key=sk-secret-value Bearer abc.def.ghi",
    )
    assert failure is not None
    assert failure.code == "worker.permission"
    assert "sk-secret-value" not in failure.evidence
    assert "abc.def.ghi" not in failure.evidence
    assert "<redacted>" in failure.evidence


def test_generic_nonzero_exit_without_evidence_is_unclassified():
    assert classify_worker_failure(returncode=1, stdout="nothing interesting\n") is None
    assert classify_worker_failure(returncode=0, stdout="nothing interesting\n") is None


def test_execution_failure_to_dict_round_trips_fields():
    failure = ExecutionFailure(
        code="worker.timeout",
        category=FailureCategory.TIMEOUT.value,
        retryable=True,
        retry_action="retry",
        evidence="timed out",
        evidence_path="/logs/agent.log",
    )
    data = failure.to_dict()
    assert data["code"] == "worker.timeout"
    assert data["category"] == "timeout"
    assert data["retryable"] is True
    assert data["retry_action"] == "retry"
    assert data["evidence"] == "timed out"
    assert data["evidence_path"] == "/logs/agent.log"
