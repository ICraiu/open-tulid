from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping
from urllib.parse import urlparse

from open_tulid.domain import DomainError

from .completion import CompletionService
from .verifier import submission_from_mapping

MAX_COMPLETION_PAYLOAD_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CompletionEndpointConfig:
    service: CompletionService
    allowed_jobs: frozenset[str] | None = None
    max_payload_bytes: int = MAX_COMPLETION_PAYLOAD_BYTES


def make_completion_handler(config: CompletionEndpointConfig) -> type[BaseHTTPRequestHandler]:
    class CompletionHandler(BaseHTTPRequestHandler):
        server_version = "open-tulid-completion/0"

        def do_POST(self) -> None:
            job_id = _job_id_from_path(self.path)
            if job_id is None:
                _write_json(self, 404, {"errors": [_error_dict("endpoint.not_found", "Endpoint was not found.")]})
                return
            if config.allowed_jobs is not None and job_id not in config.allowed_jobs:
                _write_json(self, 403, {"errors": [_error_dict("completion.job_not_bound", "Endpoint is not bound to this job.")]})
                return

            body_result = _read_request_body(self, max_bytes=config.max_payload_bytes)
            if body_result.error is not None:
                status = 413 if body_result.error["code"] == "completion.payload_too_large" else 400
                _write_json(self, status, {"errors": [body_result.error]})
                return

            try:
                payload = json.loads(body_result.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _write_json(self, 400, {"errors": [_error_dict("completion.json_malformed", "Completion payload must be valid JSON.")]})
                return
            if not isinstance(payload, Mapping):
                _write_json(self, 400, {"errors": [_error_dict("completion.payload_invalid", "Completion payload must be a JSON object.")]})
                return

            token = self.headers.get("x-open-tulid-completion-token")
            result = config.service.submit(
                job_id=job_id,
                token=token,
                submission=submission_from_mapping(payload),
            )
            if result.accepted:
                payload: dict[str, object] = {"accepted": True}
                next_state = _next_state_for_accepted(config.service, job_id)
                if next_state is not None:
                    payload["next_state"] = next_state
                _write_json(self, 200, payload)
                return

            status = _status_for_errors(result.errors)
            _write_json(self, status, {
                "accepted": False,
                "errors": [_domain_error_dict(error) for error in result.errors],
            })

        def log_message(self, format: str, *args: object) -> None:
            return

    return CompletionHandler


def serve_completion_endpoint(
    config: CompletionEndpointConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    server_factory: Callable[[tuple[str, int], type[BaseHTTPRequestHandler]], ThreadingHTTPServer] = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    server = server_factory((host, port), make_completion_handler(config))
    return server


@dataclass(frozen=True)
class _BodyReadResult:
    body: bytes
    error: Mapping[str, object] | None = None


def _read_request_body(handler: BaseHTTPRequestHandler, *, max_bytes: int) -> _BodyReadResult:
    transfer_encoding = handler.headers.get("transfer-encoding", "")
    encodings = {
        encoding.strip().lower()
        for encoding in transfer_encoding.split(",")
        if encoding.strip()
    }
    if "chunked" in encodings:
        return _read_chunked_body(handler, max_bytes=max_bytes)

    content_length = handler.headers.get("content-length")
    if content_length is None:
        return _BodyReadResult(b"", _error_dict(
            "completion.content_length_missing",
            "Completion payload requires Content-Length or Transfer-Encoding: chunked.",
        ))
    try:
        length = int(content_length)
    except ValueError:
        return _BodyReadResult(b"", _error_dict(
            "completion.content_length_invalid",
            "Content-Length must be an integer.",
        ))
    if length > max_bytes:
        return _BodyReadResult(b"", _error_dict(
            "completion.payload_too_large",
            "Completion payload exceeds 1 MiB.",
        ))
    return _BodyReadResult(handler.rfile.read(length))


def _read_chunked_body(handler: BaseHTTPRequestHandler, *, max_bytes: int) -> _BodyReadResult:
    body = bytearray()
    while True:
        size_line = handler.rfile.readline(1024)
        if not size_line:
            return _BodyReadResult(b"", _error_dict(
                "completion.chunked_malformed",
                "Chunked completion payload ended before the final chunk.",
            ))
        try:
            chunk_size = int(size_line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            return _BodyReadResult(b"", _error_dict(
                "completion.chunked_malformed",
                "Chunked completion payload has an invalid chunk size.",
            ))
        if chunk_size == 0:
            # Consume optional trailer headers.
            while True:
                trailer_line = handler.rfile.readline(1024)
                if trailer_line in {b"\r\n", b"\n", b""}:
                    break
            return _BodyReadResult(bytes(body))
        if len(body) + chunk_size > max_bytes:
            return _BodyReadResult(b"", _error_dict(
                "completion.payload_too_large",
                "Completion payload exceeds 1 MiB.",
            ))
        chunk = handler.rfile.read(chunk_size)
        if len(chunk) != chunk_size:
            return _BodyReadResult(b"", _error_dict(
                "completion.chunked_malformed",
                "Chunked completion payload ended inside a chunk.",
            ))
        body.extend(chunk)
        crlf = handler.rfile.read(2)
        if crlf != b"\r\n":
            return _BodyReadResult(b"", _error_dict(
                "completion.chunked_malformed",
                "Chunked completion payload is missing a chunk terminator.",
            ))


def _job_id_from_path(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "complete":
        return parts[1]
    return None


def _status_for_errors(errors: tuple[DomainError, ...]) -> int:
    codes = {error.code for error in errors}
    if "job.not_found" in codes:
        return 404
    if "completion.identity_mismatch" in codes or "completion.job_not_bound" in codes:
        return 403
    if "completion.job_terminal" in codes:
        return 409
    return 400


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: Mapping[str, object]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _domain_error_dict(error: DomainError) -> Mapping[str, object]:
    return {"code": error.code, "message": error.message, "location": error.location}


def _error_dict(code: str, message: str) -> Mapping[str, object]:
    return {"code": code, "message": message, "location": None}


def _next_state_for_accepted(service: CompletionService, job_id: str) -> str | None:
    loaded = service.job_store.get(job_id)
    if not loaded.accepted or loaded.job is None:
        return None
    transition = service.workflow.transitions.get(loaded.job.transition_id)
    return transition.to_state if transition is not None else None
