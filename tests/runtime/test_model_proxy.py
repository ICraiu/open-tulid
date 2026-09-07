import json
from pathlib import Path
from io import BytesIO
from threading import Thread
from urllib.error import HTTPError, URLError
from email.message import Message
from datetime import datetime, timedelta, timezone
import urllib.request

import pytest


def _utc(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)

from open_tulid.runtime import (
    SessionStatus,
    check_backend_readiness,
    FileModelProxySessionStore,
    ModelProxyService,
    ModelProxySessionStore,
    ProxyRequest,
    ProxyResponse,
    serve_model_proxy,
)
from open_tulid.runtime.model_proxy import OpenAIAdapter
from open_tulid.runtime.model_proxy import _forward_http
from open_tulid.runtime.model_proxy import _proxy_response_headers
from open_tulid.models import ModelProxyConfig
from open_tulid.models import ResourceConfig
from open_tulid.runtime import FileResourceLeaseStore
from socket_utils import can_bind_localhost


class EchoAdapter:
    def forward(self, request: ProxyRequest) -> ProxyResponse:
        return ProxyResponse(status=200, body=b"answer", headers={})


def test_model_proxy_validates_session_and_writes_full_transcript(tmp_path: Path):
    sessions = ModelProxySessionStore()
    session = sessions.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    service = ModelProxyService(
        sessions=sessions,
        adapters={"openai": EchoAdapter()},
        transcript_root=tmp_path / "proxy-logs",
        body_logging="full",
    )

    unauthorized = service.forward(
        proxy_id="openai",
        token="bad",
        request=ProxyRequest(method="POST", path="/responses", body=b"prompt", headers={}),
    )
    accepted = service.forward(
        proxy_id="openai",
        token=session.token,
        request=ProxyRequest(method="POST", path="/responses", body=b"prompt", headers={}),
    )

    assert unauthorized.status == 401
    assert accepted.status == 200
    transcript = (tmp_path / "proxy-logs" / "job-1" / "openai.jsonl").read_text(encoding="utf-8")
    assert '"request_body": "prompt"' in transcript
    assert '"response_body": "answer"' in transcript


@pytest.mark.skipif(
    not can_bind_localhost(),
    reason="localhost socket binding is unavailable in this sandbox",
)
def test_model_proxy_serves_http_requests(tmp_path: Path):
    sessions = ModelProxySessionStore()
    session = sessions.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    service = ModelProxyService(sessions=sessions, adapters={"openai": EchoAdapter()})
    server = serve_model_proxy(service, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        request = urllib.request.Request(
            f"http://{host}:{port}/proxies/openai/responses",
            data=b"prompt",
            headers={"authorization": f"Bearer {session.token}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"answer"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_backend_readiness_uses_models_endpoint_and_openai_auth():
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def opener(request, timeout):
        seen[request.full_url] = dict(request.header_items())
        assert timeout == 10
        return Response()

    results = check_backend_readiness(
        {
            "local": ModelProxyConfig(kind="local", base_url="http://127.0.0.1:8080/v1"),
            "openai": ModelProxyConfig(
                kind="openai",
                base_url="https://api.openai.com/v1",
                api_key_env="OPENAI_API_KEY",
            ),
        },
        env={"OPENAI_API_KEY": "secret"},
        opener=opener,
    )

    assert all(result.ready for result in results)
    assert "http://127.0.0.1:8080/v1/models" in seen
    assert seen["https://api.openai.com/v1/models"]["Authorization"] == "Bearer secret"


def test_backend_readiness_accepts_existing_subscription_auth_home(tmp_path: Path):
    auth_home = tmp_path / ".codex"
    auth_home.mkdir()

    results = check_backend_readiness(
        {"chatgpt-codex": ModelProxyConfig(kind="subscription", auth_home=auth_home)},
        env={},
    )

    assert results[0].ready is True


def test_openai_adapter_can_read_api_key_from_file(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "openai.key"
    key_file.write_text("file-secret\n", encoding="utf-8")
    seen = {}

    def fake_forward(base_url, request, headers):
        seen["base_url"] = base_url
        seen["headers"] = headers
        return ProxyResponse(status=200, headers={})

    monkeypatch.setattr("open_tulid.runtime.model_proxy._forward_http", fake_forward)
    adapter = OpenAIAdapter(
        ModelProxyConfig(
            kind="openai",
            base_url="https://api.openai.com/v1",
            api_key_file=key_file,
        ),
        {},
    )

    adapter.forward(ProxyRequest(method="POST", path="/responses", body=b"{}", headers={}))

    assert seen == {
        "base_url": "https://api.openai.com/v1",
        "headers": {"authorization": "Bearer file-secret"},
    }


def test_forward_http_preserves_backend_error_response(monkeypatch):
    headers = Message()
    headers["content-type"] = "application/json"

    def opener(request):
        raise HTTPError(
            request.full_url,
            400,
            "Bad Request",
            headers,
            BytesIO(b'{"error":{"message":"unsupported path"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", opener)

    response = _forward_http(
        "http://backend/v1",
        ProxyRequest(method="POST", path="/responses", body=b"{}", headers={}),
        {},
    )

    assert response.status == 400
    assert response.headers["content-type"] == "application/json"
    assert response.body == b'{"error":{"message":"unsupported path"}}'


def test_forward_http_returns_502_when_backend_is_unreachable(monkeypatch):
    def opener(request):
        raise URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", opener)

    response = _forward_http(
        "http://backend/v1",
        ProxyRequest(method="POST", path="/chat/completions", body=b"{}", headers={}),
        {},
    )

    assert response.status == 502
    assert b"backend_unavailable" in response.body


def test_proxy_response_headers_strip_hop_by_hop_headers_for_streaming_response():
    headers = _proxy_response_headers(
        {
            "content-type": "text/event-stream",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
            "content-length": "999",
        },
        body_length=None,
    )

    assert headers == {"content-type": "text/event-stream"}


def test_proxy_response_headers_set_content_length_for_buffered_response():
    headers = _proxy_response_headers(
        {
            "content-type": "application/json",
            "transfer-encoding": "chunked",
        },
        body_length=12,
    )

    assert headers == {
        "content-type": "application/json",
        "content-length": "12",
    }


def test_model_proxy_rejects_session_without_live_resource_lease(tmp_path: Path):
    sessions = ModelProxySessionStore()
    session = sessions.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    leases = FileResourceLeaseStore(
        tmp_path / "leases",
        {"remote-llm": ResourceConfig(kind="model", capacity=1)},
    )
    service = ModelProxyService(
        sessions=sessions,
        adapters={"openai": EchoAdapter()},
        lease_store=leases,
    )

    response = service.forward(
        proxy_id="openai",
        token=session.token,
        request=ProxyRequest(method="POST", path="/responses", body=b"prompt", headers={}),
    )

    assert response.status == 403


def test_file_model_proxy_session_store_never_overwrites_existing_token(tmp_path: Path, monkeypatch):
    store = FileModelProxySessionStore(tmp_path / "sessions")
    tokens = iter(("duplicate", "duplicate", "fresh"))
    monkeypatch.setattr("open_tulid.runtime.model_proxy.secrets.token_urlsafe", lambda size: next(tokens))

    first = store.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    second = store.issue(job_id="job-2", worker_id="codex", proxy_id="openai", resource_id="remote-llm")

    assert first.token == "duplicate"
    assert second.token == "fresh"
    assert store.get("duplicate").session.job_id == "job-1"
    assert store.get("fresh").session.job_id == "job-2"


def test_model_proxy_session_stores_expire_issued_sessions(tmp_path: Path):
    memory = ModelProxySessionStore(ttl_seconds=-1)
    memory_session = memory.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    assert memory_session.issued_at is not None
    assert memory_session.expires_at is not None
    assert memory.get(memory_session.token).status is SessionStatus.EXPIRED

    files = FileModelProxySessionStore(tmp_path / "sessions", ttl_seconds=-1)
    file_session = files.issue(job_id="job-2", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    assert file_session.issued_at is not None
    assert file_session.expires_at is not None
    assert files.get(file_session.token).status is SessionStatus.EXPIRED
    assert not (tmp_path / "sessions" / f"{file_session.token}.json").exists()


class FakeClock:
    def __init__(self, now):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, seconds):
        from datetime import timedelta
        self._now = self._now + timedelta(seconds=seconds)


def test_session_stores_price_sessions_to_explicit_expiry_with_injected_clock(tmp_path):
    start = _utc(2026, 9, 7, 12, 0, 0)
    clock = FakeClock(start)
    memory = ModelProxySessionStore(clock=clock, ttl_seconds=3600)
    files = FileModelProxySessionStore(tmp_path / "sessions", clock=clock, ttl_seconds=3600)

    session = memory.issue(
        job_id="job-1",
        worker_id="codex",
        proxy_id="openai",
        resource_id="remote-llm",
        attempt_id="job-1@2",
        expires_at=(start + timedelta(hours=2)).isoformat(),
    )
    assert session.attempt_id == "job-1@2"
    assert session.expires_at == (start + timedelta(hours=2)).isoformat()

    clock.advance(59 * 60)
    assert memory.get(session.token).status is SessionStatus.VALID
    clock.advance(2 * 60)
    assert memory.get(session.token).status is SessionStatus.VALID
    clock.advance(58 * 60)
    assert memory.get(session.token).status is SessionStatus.VALID
    clock.advance(60)
    assert memory.get(session.token).status is SessionStatus.EXPIRED

    file_session = files.issue(
        job_id="job-2",
        worker_id="codex",
        proxy_id="openai",
        resource_id="remote-llm",
        expires_at=(clock() - timedelta(seconds=1)).isoformat(),
    )
    assert files.get(file_session.token).status is SessionStatus.EXPIRED


def test_session_store_returns_unknown_for_missing_token():
    memory = ModelProxySessionStore()
    assert memory.get("no-such-token").status is SessionStatus.UNKNOWN


def test_revoke_attempt_does_not_revoke_newly_issued_credential(tmp_path):
    start = _utc(2026, 9, 7, 12, 0, 0)
    clock = FakeClock(start)
    memory = ModelProxySessionStore(clock=clock)
    files = FileModelProxySessionStore(tmp_path / "sessions", clock=clock)

    old_memory = memory.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm", attempt_id="job-1@1")
    new_memory = memory.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm", attempt_id="job-1@2")
    memory.revoke_attempt("job-1@1")
    assert memory.get(old_memory.token).status is SessionStatus.UNKNOWN
    assert memory.get(new_memory.token).status is SessionStatus.VALID

    old_file = files.issue(job_id="job-2", worker_id="codex", proxy_id="openai", resource_id="remote-llm", attempt_id="job-2@1")
    new_file = files.issue(job_id="job-2", worker_id="codex", proxy_id="openai", resource_id="remote-llm", attempt_id="job-2@2")
    files.revoke_attempt("job-2@1")
    assert files.get(old_file.token).status is SessionStatus.UNKNOWN
    assert files.get(new_file.token).status is SessionStatus.VALID
    assert not (tmp_path / "sessions" / f"{old_file.token}.json").exists()
    assert (tmp_path / "sessions" / f"{new_file.token}.json").exists()


def test_model_proxy_logs_rejection_evidence_without_tokens(tmp_path):
    sessions = ModelProxySessionStore(clock=FakeClock(_utc(2026, 9, 7, 12, 0, 0)))
    session = sessions.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    service = ModelProxyService(
        sessions=sessions,
        adapters={"openai": EchoAdapter()},
        transcript_root=tmp_path / "proxy-logs",
    )
    request = ProxyRequest(method="POST", path="/responses", body=b"prompt", headers={})

    unknown = service.forward(proxy_id="openai", token="bad", request=request)
    wrong_proxy = service.forward(proxy_id="qwen", token=session.token, request=request)

    assert unknown.status == 401
    assert wrong_proxy.status == 401
    records = (tmp_path / "proxy-logs" / "rejections.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(records) == 2
    first = json.loads(records[0])
    assert first["reason"] == "session_unknown"
    assert first["job_id"] is None
    assert "token" not in first
    second = json.loads(records[1])
    assert second["reason"] == "wrong_proxy"
    assert second["job_id"] == "job-1"
    assert second["attempt_id"] is None
    assert "token" not in second


def test_model_proxy_rejects_expired_session_and_records_reason(tmp_path):
    sessions = ModelProxySessionStore(ttl_seconds=-1)
    session = sessions.issue(job_id="job-1", worker_id="codex", proxy_id="openai", resource_id="remote-llm")
    service = ModelProxyService(sessions=sessions, adapters={"openai": EchoAdapter()}, transcript_root=tmp_path / "logs")
    request = ProxyRequest(method="POST", path="/responses", body=b"prompt", headers={})

    response = service.forward(proxy_id="openai", token=session.token, request=request)

    assert response.status == 401
    records = (tmp_path / "logs" / "rejections.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(records[0])["reason"] == "session_expired"
