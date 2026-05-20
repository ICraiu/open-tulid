from __future__ import annotations

import http.client
import json
import threading
from dataclasses import dataclass

from open_tulid.domain import DomainError
from open_tulid.runtime.completion import CompletionResult
from open_tulid.runtime.completion_http import CompletionEndpointConfig, serve_completion_endpoint
from open_tulid.runtime.verifier import CompletionSubmission


@dataclass
class _CapturedSubmission:
    job_id: str
    token: str | None
    submission: CompletionSubmission


class _RejectingCompletionService:
    def __init__(self) -> None:
        self.captured: _CapturedSubmission | None = None

    def submit(
        self,
        *,
        job_id: str,
        token: str | None,
        submission: CompletionSubmission,
    ) -> CompletionResult:
        self.captured = _CapturedSubmission(job_id, token, submission)
        return CompletionResult(
            accepted=False,
            errors=(DomainError("test.rejected", "test rejection"),),
        )


def test_completion_endpoint_accepts_chunked_json_payload() -> None:
    service = _RejectingCompletionService()
    server = serve_completion_endpoint(CompletionEndpointConfig(service=service), host="127.0.0.1", port=0)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({
            "summary": "done",
            "changed_files": ["src/game/GameState.ts"],
            "validation_evidence": {
                "tests_pass": "npm test passed",
                "project_build": "npm run build passed",
            },
        }).encode()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/jobs/job-1/complete",
            body=(chunk for chunk in (body[:10], body[10:])),
            headers={
                "content-type": "application/json",
                "x-open-tulid-completion-token": "token-1",
            },
            encode_chunked=True,
        )
        response = connection.getresponse()
        response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 400
    assert service.captured == _CapturedSubmission(
        job_id="job-1",
        token="token-1",
        submission=CompletionSubmission(
            summary="done",
            changed_files=("src/game/GameState.ts",),
            validation_evidence={
                "tests_pass": "npm test passed",
                "project_build": "npm run build passed",
            },
        ),
    )


def test_completion_endpoint_rejects_missing_body_framing() -> None:
    service = _RejectingCompletionService()
    server = serve_completion_endpoint(CompletionEndpointConfig(service=service), host="127.0.0.1", port=0)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.putrequest("POST", "/jobs/job-1/complete")
        connection.putheader("content-type", "application/json")
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 400
    assert payload["errors"][0]["code"] == "completion.content_length_missing"
    assert service.captured is None
