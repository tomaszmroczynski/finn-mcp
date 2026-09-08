from __future__ import annotations

import pytest

from finn_mcp.http_server import BearerAuthMiddleware, _access_token


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _request(token: str | None) -> int:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = BearerAuthMiddleware(_ok_app, "x" * 32)
    await middleware(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_bearer_auth_rejects_missing_and_wrong_token():
    assert await _request(None) == 401
    assert await _request("wrong") == 401


async def test_bearer_auth_accepts_matching_token():
    assert await _request("x" * 32) == 204


def test_access_token_must_be_long(monkeypatch):
    monkeypatch.setenv("FINN_MCP_ACCESS_TOKEN", "short")
    with pytest.raises(RuntimeError):
        _access_token()


def test_allowed_hosts_come_from_the_environment(monkeypatch):
    """The public domain must not live in the source; it arrives per deployment."""
    from finn_mcp.http_server import _configured_hosts, _transport_security

    monkeypatch.delenv("FINN_MCP_ALLOWED_HOST", raising=False)
    assert _configured_hosts() == []
    assert "127.0.0.1:*" in _transport_security().allowed_hosts

    monkeypatch.setenv("FINN_MCP_ALLOWED_HOST", " finn.example.no ; other.example ")
    hosts = _transport_security().allowed_hosts
    for expected in ("finn.example.no", "finn.example.no:*", "other.example:*", "localhost:*"):
        assert expected in hosts


def test_create_app_applies_transport_security(monkeypatch):
    from finn_mcp.http_server import create_app
    from finn_mcp.server import mcp

    monkeypatch.setenv("FINN_MCP_ACCESS_TOKEN", "t" * 32)
    monkeypatch.setenv("FINN_MCP_ALLOWED_HOST", "finn.example.no")
    app = create_app()
    assert app is not None
    assert "finn.example.no" in mcp.settings.transport_security.allowed_hosts
