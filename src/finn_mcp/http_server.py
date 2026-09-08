"""Authenticated Streamable HTTP entry point, for running behind a reverse proxy.

Upstream ships a stdio server. This module is what lets the same server sit in
a container behind Cloudflare and a Synology reverse proxy: one bearer token
for every request, and the proxy's public host name admitted past the SDK's
DNS-rebinding guard.

Run with:  uvicorn finn_mcp.http_server:create_app --factory
"""

from __future__ import annotations

import hmac
import os

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .server import mcp

# The SDK's own defaults, repeated so that naming a public host does not
# quietly drop loopback access for health checks and local debugging.
_LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def _access_token() -> str:
    token = os.environ.get("FINN_MCP_ACCESS_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError(
            "FINN_MCP_ACCESS_TOKEN must be set to at least 32 characters"
        )
    return token


def _configured_hosts() -> list[str]:
    """Public host names from FINN_MCP_ALLOWED_HOST, comma- or semicolon-separated.

    Each is admitted with and without a port, because the Host header behind a
    proxy may carry either form.
    """
    raw = os.environ.get("FINN_MCP_ALLOWED_HOST", "")
    hosts: list[str] = []
    for item in raw.replace(";", ",").split(","):
        host = item.strip()
        if host:
            hosts.extend((host, f"{host}:*"))
    return hosts


def _transport_security() -> TransportSecuritySettings:
    # The SDK rejects any request whose Host header it does not recognise
    # with 421 Invalid Host header. Behind a proxy that header is our public
    # domain, so it has to be admitted here -- from the environment, not
    # hardcoded, so the source carries no deployment-specific name.
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*_LOCAL_HOSTS, *_configured_hosts()],
    )


class BearerAuthMiddleware:
    """Require one deployment-local bearer token for every HTTP request."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        authenticated = (
            scheme.lower() == "bearer"
            and bool(supplied)
            and hmac.compare_digest(supplied, self.token)
        )
        if not authenticated:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_app() -> ASGIApp:
    """Build the authenticated Streamable HTTP application for uvicorn."""
    mcp.settings.transport_security = _transport_security()
    return BearerAuthMiddleware(mcp.streamable_http_app(), _access_token())
