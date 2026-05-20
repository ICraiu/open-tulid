from __future__ import annotations

import json
import os
import secrets
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Iterable
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
import fcntl

from open_tulid.models import ModelProxyConfig


@dataclass(frozen=True)
class ModelProxySession:
    token: str
    job_id: str
    worker_id: str
    proxy_id: str
    resource_id: str
    issued_at: str | None = None
    expires_at: str | None = None

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return (now or datetime.now(timezone.utc)) >= expiry


class ModelProxySessionStore:
    def __init__(self, *, ttl_seconds: int = 3600) -> None:
        self._sessions: dict[str, ModelProxySession] = {}
        self.ttl_seconds = ttl_seconds

    def issue(self, *, job_id: str, worker_id: str, proxy_id: str, resource_id: str) -> ModelProxySession:
        session = _new_session(
            job_id=job_id,
            worker_id=worker_id,
            proxy_id=proxy_id,
            resource_id=resource_id,
            ttl_seconds=self.ttl_seconds,
        )
        self._sessions[session.token] = session
        return session

    def get(self, token: str) -> ModelProxySession | None:
        session = self._sessions.get(token)
        if session is not None and session.expired():
            del self._sessions[token]
            return None
        return session

    def revoke_job(self, job_id: str) -> None:
        for token, session in tuple(self._sessions.items()):
            if session.job_id == job_id:
                del self._sessions[token]


class FileModelProxySessionStore:
    def __init__(self, root: Path, *, ttl_seconds: int = 3600) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds

    def issue(self, *, job_id: str, worker_id: str, proxy_id: str, resource_id: str) -> ModelProxySession:
        with self._locked():
            while True:
                session = _new_session(
                    job_id=job_id,
                    worker_id=worker_id,
                    proxy_id=proxy_id,
                    resource_id=resource_id,
                    ttl_seconds=self.ttl_seconds,
                )
                path = self.root / f"{session.token}.json"
                if not path.exists():
                    break
            fd, tmp_name = tempfile.mkstemp(prefix=".session.", suffix=".tmp", dir=str(self.root), text=True)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(session.__dict__, handle, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
                _fsync_directory(self.root)
            finally:
                tmp_path.unlink(missing_ok=True)
            return session

    def get(self, token: str) -> ModelProxySession | None:
        path = self.root / f"{token}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            session = ModelProxySession(**payload)
            if session.expired():
                path.unlink(missing_ok=True)
                return None
            return session
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            return None

    def revoke_job(self, job_id: str) -> None:
        with self._locked():
            for path in self.root.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if payload.get("job_id") == job_id:
                    path.unlink(missing_ok=True)

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _new_session(
    *,
    job_id: str,
    worker_id: str,
    proxy_id: str,
    resource_id: str,
    ttl_seconds: int,
) -> ModelProxySession:
    now = datetime.now(timezone.utc)
    return ModelProxySession(
        token=secrets.token_urlsafe(32),
        job_id=job_id,
        worker_id=worker_id,
        proxy_id=proxy_id,
        resource_id=resource_id,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
    )


@dataclass(frozen=True)
class ProxyRequest:
    method: str
    path: str
    body: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""
    chunks: Iterable[bytes] | None = None


class BackendAdapter(Protocol):
    def forward(self, request: ProxyRequest) -> ProxyResponse:
        ...


class LocalModelAdapter:
    def __init__(self, config: ModelProxyConfig):
        self.config = config

    def forward(self, request: ProxyRequest) -> ProxyResponse:
        return _forward_http(self.config.base_url, request, {})


class OpenAIAdapter:
    def __init__(self, config: ModelProxyConfig, env: Mapping[str, str]):
        self.config = config
        self.env = env

    def forward(self, request: ProxyRequest) -> ProxyResponse:
        api_key = _openai_api_key(self.config, self.env)
        assert self.config.base_url is not None
        return _forward_http(self.config.base_url, request, {"authorization": f"Bearer {api_key}"})


@dataclass(frozen=True)
class BackendReadiness:
    proxy_id: str
    ready: bool
    status: int | None = None
    error: str | None = None


def check_backend_readiness(
    proxies: Mapping[str, ModelProxyConfig],
    *,
    env: Mapping[str, str],
    opener=urllib.request.urlopen,
) -> tuple[BackendReadiness, ...]:
    results: list[BackendReadiness] = []
    for proxy_id, config in proxies.items():
        if config.kind == "subscription":
            ready = config.auth_home is not None and config.auth_home.is_dir()
            results.append(BackendReadiness(
                proxy_id=proxy_id,
                ready=ready,
                error=None if ready else f"subscription auth home is unavailable: {config.auth_home}",
            ))
            continue
        headers = {}
        if config.kind == "openai":
            try:
                api_key = _openai_api_key(config, env)
            except RuntimeError as exc:
                results.append(BackendReadiness(
                    proxy_id=proxy_id,
                    ready=False,
                    error=str(exc),
                ))
                continue
            headers["authorization"] = f"Bearer {api_key}"
        assert config.base_url is not None
        request = urllib.request.Request(
            config.base_url.rstrip("/") + "/models",
            headers=headers,
            method="GET",
        )
        try:
            with opener(request, timeout=10) as response:
                status = int(response.status)
                results.append(BackendReadiness(proxy_id=proxy_id, ready=200 <= status < 300, status=status))
        except HTTPError as exc:
            results.append(BackendReadiness(proxy_id=proxy_id, ready=False, status=exc.code, error=str(exc)))
        except (URLError, OSError) as exc:
            results.append(BackendReadiness(proxy_id=proxy_id, ready=False, error=str(exc)))
    return tuple(results)


def _openai_api_key(config: ModelProxyConfig, env: Mapping[str, str]) -> str:
    if config.api_key_env is not None:
        api_key = env.get(config.api_key_env)
        if not api_key:
            raise RuntimeError(f"missing environment variable {config.api_key_env}")
        return api_key
    if config.api_key_file is not None:
        try:
            api_key = config.api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read API key file {config.api_key_file}") from exc
        if not api_key:
            raise RuntimeError(f"API key file {config.api_key_file} is empty")
        return api_key
    raise RuntimeError("openai proxy is missing authentication config")


class ModelProxyService:
    def __init__(
        self,
        *,
        sessions: ModelProxySessionStore,
        adapters: Mapping[str, BackendAdapter],
        lease_store: object | None = None,
        transcript_root: Path | None = None,
        body_logging: str = "metadata",
    ) -> None:
        self.sessions = sessions
        self.adapters = adapters
        self.lease_store = lease_store
        self.transcript_root = transcript_root
        self.body_logging = body_logging

    def forward(self, *, proxy_id: str, token: str, request: ProxyRequest) -> ProxyResponse:
        session = self.sessions.get(token)
        if session is None or session.proxy_id != proxy_id:
            return ProxyResponse(status=401, body=b"unauthorized", headers={})
        if self.lease_store is not None and not self.lease_store.job_holds((session.resource_id,), session.job_id):
            return ProxyResponse(status=403, body=b"resource lease not held", headers={})
        adapter = self.adapters.get(proxy_id)
        if adapter is None:
            return ProxyResponse(status=404, body=b"unknown proxy", headers={})
        response = adapter.forward(request)
        return self._with_transcript(session, request, response)

    def _with_transcript(
        self,
        session: ModelProxySession,
        request: ProxyRequest,
        response: ProxyResponse,
    ) -> ProxyResponse:
        if response.chunks is None:
            self._write_transcript(session, request, response, response.body)
            return response

        def chunks():
            captured = bytearray()
            for chunk in response.chunks or ():
                captured.extend(chunk)
                yield chunk
            self._write_transcript(session, request, response, bytes(captured))

        return ProxyResponse(
            status=response.status,
            body=b"",
            headers=response.headers,
            chunks=chunks(),
        )

    def _write_transcript(
        self,
        session: ModelProxySession,
        request: ProxyRequest,
        response: ProxyResponse,
        response_body: bytes,
    ) -> None:
        if self.transcript_root is None or self.body_logging == "none":
            return
        path = self.transcript_root / session.job_id / f"{session.proxy_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "proxy_id": session.proxy_id,
            "job_id": session.job_id,
            "worker_id": session.worker_id,
            "method": request.method,
            "path": request.path,
            "status": response.status,
            "request_bytes": len(request.body),
            "response_bytes": len(response_body),
        }
        if self.body_logging == "full":
            payload["request_body"] = request.body.decode("utf-8", errors="replace")
            payload["response_body"] = response_body.decode("utf-8", errors="replace")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")


def make_model_proxy_handler(service: ModelProxyService) -> type[BaseHTTPRequestHandler]:
    class ModelProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def _forward(self) -> None:
            parts = self.path.lstrip("/").split("/", 2)
            if len(parts) < 2 or parts[0] != "proxies":
                self.send_error(404)
                return
            proxy_id = parts[1]
            path = "/" + parts[2] if len(parts) > 2 else "/"
            raw_auth = self.headers.get("authorization", "")
            token = raw_auth.removeprefix("Bearer ").strip() if raw_auth.startswith("Bearer ") else ""
            length = int(self.headers.get("content-length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            response = service.forward(
                proxy_id=proxy_id,
                token=token,
                request=ProxyRequest(
                    method=self.command,
                    path=path,
                    body=body,
                    headers={key: value for key, value in self.headers.items()},
                ),
            )
            self.send_response(response.status)
            response_headers = _proxy_response_headers(
                response.headers,
                body_length=None if response.chunks is not None else len(response.body),
            )
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            if response.chunks is not None:
                for chunk in response.chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ModelProxyHandler


def serve_model_proxy(
    service: ModelProxyService,
    *,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_model_proxy_handler(service))


def _forward_http(base_url: str, request: ProxyRequest, extra_headers: Mapping[str, str]) -> ProxyResponse:
    url = base_url.rstrip("/") + "/" + request.path.lstrip("/")
    headers = {key: value for key, value in request.headers.items() if key.lower() != "authorization"}
    headers.update(extra_headers)
    raw = urllib.request.Request(url, data=request.body or None, headers=headers, method=request.method)
    try:
        response = urllib.request.urlopen(raw)
    except HTTPError as exc:
        return ProxyResponse(
            status=exc.code,
            headers=dict(exc.headers.items()),
            body=exc.read(),
        )
    except (URLError, OSError) as exc:
        return ProxyResponse(
            status=502,
            headers={"content-type": "application/json"},
            body=json.dumps({
                "error": {
                    "message": f"model proxy backend request failed: {exc}",
                    "type": "backend_unavailable",
                },
            }).encode("utf-8"),
        )

    def chunks():
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            response.close()

    return ProxyResponse(
        status=response.status,
        headers=dict(response.headers.items()),
        chunks=chunks(),
    )


_HOP_BY_HOP_RESPONSE_HEADERS = frozenset({
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})


def _proxy_response_headers(headers: Mapping[str, str], *, body_length: int | None) -> dict[str, str]:
    sanitized = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }
    if body_length is not None:
        sanitized["content-length"] = str(body_length)
    return sanitized


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
