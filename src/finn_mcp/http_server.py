"""Authenticated Streamable HTTP entry point, for running behind a reverse proxy.

Upstream ships a stdio server. This module is what lets the same server sit in
a container behind Cloudflare and a Synology reverse proxy: a bearer token on
every request, and the proxy's public host name admitted past the SDK's
DNS-rebinding guard.

Two modes, chosen by FINN_MCP_PUBLIC_URL:

* unset -- one static deployment token, checked by BearerAuthMiddleware below.
  This is what has run in production since July.
* set -- the server is also its own OAuth 2.1 authorization server (see
  oauth.py), so clients that cannot carry a static token, such as claude.ai
  custom connectors, can log in through a browser. The static token keeps
  working alongside; nothing that used it has to change.

Run with:  uvicorn finn_mcp.http_server:create_app --factory
"""

from __future__ import annotations

import hmac
import os

from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config
from .server import mcp

# The SDK's own defaults, repeated so that naming a public host does not
# quietly drop loopback access for health checks and local debugging.
_LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOCAL_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")


def _access_token() -> str:
    token = os.environ.get("FINN_MCP_ACCESS_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError(
            "FINN_MCP_ACCESS_TOKEN must be set to at least 32 characters"
        )
    return token


def _configured_hosts() -> list[str]:
    """Public host names from FINN_MCP_ALLOWED_HOST, comma- or semicolon-separated."""
    raw = os.environ.get("FINN_MCP_ALLOWED_HOST", "")
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _transport_security() -> TransportSecuritySettings:
    # The SDK rejects any request whose Host header it does not recognise
    # with 421 Invalid Host header. Behind a proxy that header is our public
    # domain, so it has to be admitted here -- from the environment, not
    # hardcoded, so the source carries no deployment-specific name.
    #
    # Each host is admitted with and without a port, because the Host header
    # behind a proxy may carry either form, and its https origin is admitted
    # too, mirroring what production ran with before this was configurable.
    hosts = _configured_hosts()
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*_LOCAL_HOSTS, *(h for host in hosts for h in (host, f"{host}:*"))],
        allowed_origins=[*_LOCAL_ORIGINS, *(f"https://{host}" for host in hosts)],
    )


class BearerAuthMiddleware:
    """Require one deployment-local bearer token for every HTTP request.

    The static-token mode. Kept as the plain, dependency-free path: when no
    public URL is configured there is no authorization server to expose, and
    a middleware that refuses everything without the token is the whole story.
    """

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


def _enable_oauth(static_token: str) -> None:
    """Turn the shared FastMCP instance into an OAuth authorization server.

    Injected after construction, the same way transport security is, so
    server.py stays transport-agnostic and identical to a stdio deployment.
    The SDK mounts /authorize, /token, /register, /revoke and both
    /.well-known documents from these settings; only /login is ours.
    """
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    from .oauth import SCOPE, DualTokenVerifier, FinnAuthorizationServer, Store, login_routes

    public_url = config.PUBLIC_URL
    resource = f"{public_url}/mcp"
    provider = FinnAuthorizationServer(
        Store(config.oauth_db_path()), public_url, resource, config.LOGIN_SECRET
    )
    mcp._auth_server_provider = provider
    mcp._token_verifier = DualTokenVerifier(static_token, provider)
    mcp.settings.auth = AuthSettings(
        issuer_url=public_url,
        resource_server_url=resource,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=None,
    )

    # Registered once per process; the handlers fetch the current provider
    # from the instance on every request, so re-running create_app (tests,
    # a future reload) re-configures them without re-registering routes.
    if not any(getattr(r, "path", None) == "/login" for r in mcp._custom_starlette_routes):
        get_login, post_login = login_routes(lambda: mcp._auth_server_provider)
        mcp.custom_route("/login", methods=["GET"], include_in_schema=False)(get_login)
        mcp.custom_route("/login", methods=["POST"], include_in_schema=False)(post_login)


def create_app() -> ASGIApp:
    """Build the authenticated Streamable HTTP application for uvicorn."""
    mcp.settings.transport_security = _transport_security()
    static_token = _access_token()

    if not config.PUBLIC_URL:
        return BearerAuthMiddleware(mcp.streamable_http_app(), static_token)

    _enable_oauth(static_token)
    return mcp.streamable_http_app()
