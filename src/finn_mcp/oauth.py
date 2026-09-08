"""OAuth 2.1 authorization server for the HTTP transport, self-contained.

claude.ai's custom connectors, and any other MCP client following the 2025-06-18
authorization spec, will not send a static bearer token: they discover an
authorization server, register themselves, send the user through a browser
login, and exchange a PKCE-protected code for short-lived tokens. There is no
external identity provider here -- this server is its own authorization server,
in the same container, with its state in a small SQLite file under /data.

What the SDK already does, and is not repeated here: PKCE S256 enforcement,
redirect_uri validation against the registration, the /authorize, /token,
/register and /revoke handlers, and both /.well-known documents. What this
module provides is the storage behind those handlers, the one screen a person
sees (a single secret, entered once per connector), and a token verifier that
accepts *either* an issued token or the deployment's static bearer token, so
the existing clients keep working unchanged.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenVerifier,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import config

SCOPE = "finn"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending (key TEXT PRIMARY KEY, client_id TEXT NOT NULL, data TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS codes   (key TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS access  (key TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS refresh (key TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at INTEGER NOT NULL);
"""

_EXPIRING = ("pending", "codes", "access", "refresh")


def _now() -> int:
    return int(time.time())


class Store:
    """SQLite behind the provider. Every read sweeps expired rows in passing."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        memory = str(self.path) == ":memory:"
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        if not memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _sweep(self) -> None:
        now = _now()
        for table in _EXPIRING:
            self._conn.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (now,))
        self._conn.commit()

    def put(self, table: str, key: str, data: dict[str, Any], ttl: int) -> None:
        assert table in _EXPIRING
        self._conn.execute(
            f"INSERT OR REPLACE INTO {table} (key, data, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), _now() + ttl),
        )
        self._conn.commit()

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        assert table in _EXPIRING
        self._sweep()
        row = self._conn.execute(f"SELECT data FROM {table} WHERE key = ?", (key,)).fetchone()
        return json.loads(row["data"]) if row else None

    def delete(self, table: str, key: str) -> None:
        assert table in _EXPIRING
        self._conn.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
        self._conn.commit()

    def put_client(self, client: OAuthClientInformationFull) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO clients (client_id, data) VALUES (?, ?)",
            (client.client_id, client.model_dump_json()),
        )
        self._conn.commit()

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._conn.execute("SELECT data FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["data"]) if row else None

    def put_pending(self, req: str, client_id: str, params: dict[str, Any], ttl: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO pending (key, client_id, data, expires_at) VALUES (?, ?, ?, ?)",
            (req, client_id, json.dumps(params), _now() + ttl),
        )
        self._conn.commit()

    def get_pending(self, req: str) -> tuple[str, dict[str, Any]] | None:
        self._sweep()
        row = self._conn.execute("SELECT client_id, data FROM pending WHERE key = ?", (req,)).fetchone()
        return (row["client_id"], json.loads(row["data"])) if row else None


class FinnAuthorizationServer(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """The provider the SDK's handlers call into.

    ``authorize`` does not decide anything: it parks the request and sends the
    browser to /login. The decision is made there, by whoever knows the login
    secret, and only then is a code minted.
    """

    def __init__(self, store: Store, public_url: str, resource: str, login_secret: str):
        if len(login_secret) < 12:
            raise RuntimeError("the OAuth login secret must be at least 12 characters")
        self.store = store
        self.public_url = public_url.rstrip("/")
        self.resource = resource.rstrip("/")
        self._login_secret = login_secret

    # -- clients: dynamic registration is open; it grants nothing by itself ----
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info)

    # -- authorization ---------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = secrets.token_urlsafe(24)
        self.store.put_pending(
            req,
            client.client_id,
            {
                "state": params.state,
                "scopes": params.scopes or [SCOPE],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
            ttl=config.OAUTH_LOGIN_TTL_SECONDS,
        )
        return f"{self.public_url}/login?{urlencode({'req': req})}"

    def complete_login(self, req: str, secret: str) -> str | None:
        """Called by the login page. Returns the redirect URL, or None if refused."""
        pending = self.store.get_pending(req)
        if pending is None:
            return None
        client_id, params = pending
        if not hmac.compare_digest(secret.encode(), self._login_secret.encode()):
            return None
        self.store.delete("pending", req)
        code = secrets.token_urlsafe(32)
        record = AuthorizationCode(
            code=code,
            scopes=params["scopes"],
            expires_at=_now() + config.OAUTH_CODE_TTL_SECONDS,
            client_id=client_id,
            code_challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=params.get("resource"),
        )
        self.store.put("codes", code, record.model_dump(mode="json"), config.OAUTH_CODE_TTL_SECONDS)
        query: dict[str, str] = {"code": code}
        if params.get("state"):
            query["state"] = params["state"]
        sep = "&" if "?" in params["redirect_uri"] else "?"
        return f"{params['redirect_uri']}{sep}{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.store.get("codes", authorization_code)
        if data is None or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(data)

    # -- tokens ----------------------------------------------------------------
    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.store.put(
            "access",
            access,
            AccessToken(
                token=access,
                client_id=client_id,
                scopes=scopes,
                expires_at=_now() + config.OAUTH_ACCESS_TTL_SECONDS,
                resource=resource,
            ).model_dump(mode="json"),
            config.OAUTH_ACCESS_TTL_SECONDS,
        )
        self.store.put(
            "refresh",
            refresh,
            {
                **RefreshToken(
                    token=refresh,
                    client_id=client_id,
                    scopes=scopes,
                    expires_at=_now() + config.OAUTH_REFRESH_TTL_SECONDS,
                ).model_dump(mode="json"),
                "resource": resource,
            },
            config.OAUTH_REFRESH_TTL_SECONDS,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=config.OAUTH_ACCESS_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: a code that can be replayed is a code that can be stolen.
        self.store.delete("codes", authorization_code.code)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self.store.get("refresh", refresh_token)
        if data is None or data["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate({k: data[k] for k in ("token", "client_id", "scopes", "expires_at")})

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotation, as OAuth 2.1 requires for public clients: the old one dies here.
        data = self.store.get("refresh", refresh_token.token) or {}
        self.store.delete("refresh", refresh_token.token)
        return self._issue(client.client_id, scopes or refresh_token.scopes, data.get("resource"))

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.store.get("access", token)
        if data is None:
            return None
        record = AccessToken.model_validate(data)
        # Audience binding (RFC 8707): a token minted for some other resource is
        # not ours to honour, however valid it looks.
        if record.resource and record.resource.rstrip("/") != self.resource:
            return None
        return record

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.delete("refresh" if isinstance(token, RefreshToken) else "access", token.token)


class DualTokenVerifier(TokenVerifier):
    """Static deployment token first, issued OAuth tokens second.

    The site on Vercel, Claude Code and Codex keep using the one static token;
    connectors that went through the browser get their own. Both land here.
    """

    def __init__(self, static_token: str, provider: FinnAuthorizationServer | None):
        self._static = static_token
        self._provider = provider

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._static and hmac.compare_digest(token.encode(), self._static.encode()):
            return AccessToken(token="static", client_id="deployment", scopes=[SCOPE], expires_at=None)
        if self._provider is None:
            return None
        return await self._provider.load_access_token(token)


# -- the one screen a person sees ---------------------------------------------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>finn-mcp: authorize</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0}}
form{{background:#1b1b1b;padding:2rem;border-radius:12px;max-width:26rem;width:100%;box-sizing:border-box}}
input{{width:100%;box-sizing:border-box;padding:.7rem;border-radius:8px;border:1px solid #444;background:#0d0d0d;color:#eee;font-size:1rem}}
button{{margin-top:1rem;width:100%;padding:.8rem;border:0;border-radius:8px;background:#e8642c;color:#fff;font-size:1rem;cursor:pointer}}
.err{{color:#ff8a80;margin:0 0 1rem}} small{{color:#888}}
</style></head><body>
<form method="post" action="/login" autocomplete="off">
<h1 style="margin:0 0 .5rem;font-size:1.25rem">Authorize <em>{client}</em></h1>
<p><small>This client asks for access to finn-mcp. Enter the login secret to allow it.</small></p>
{error}<input type="hidden" name="req" value="{req}">
<input type="password" name="secret" placeholder="login secret" required autofocus>
<button type="submit">Allow</button>
</form></body></html>"""

_GONE = "<p>This authorization request is unknown or has expired. Start again from the client.</p>"


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def login_routes(get_provider: Callable[[], FinnAuthorizationServer | None]):
    """GET shows the form for a parked request; POST checks the secret and redirects.

    The provider is looked up per request rather than captured, because
    Starlette bakes a route's endpoint in at registration and the shared
    FastMCP instance may be re-configured after that (tests do; a future
    reload might).
    """

    def _page(provider: FinnAuthorizationServer, req: str, error: str) -> HTMLResponse | None:
        pending = provider.store.get_pending(req) if req else None
        if pending is None:
            return None
        client = provider.store.get_client(pending[0])
        name = client.client_name if client and client.client_name else pending[0]
        status = 401 if error else 200
        return HTMLResponse(_PAGE.format(client=_escape(name), req=_escape(req), error=error), status_code=status)

    async def get_login(request: Request) -> Response:
        provider = get_provider()
        if provider is None:
            return HTMLResponse(_GONE, status_code=404)
        page = _page(provider, request.query_params.get("req", ""), "")
        return page or HTMLResponse(_GONE, status_code=400)

    async def post_login(request: Request) -> Response:
        provider = get_provider()
        if provider is None:
            return HTMLResponse(_GONE, status_code=404)
        form = await request.form()
        req = str(form.get("req", ""))
        target = provider.complete_login(req, str(form.get("secret", "")))
        if target is not None:
            return RedirectResponse(target, status_code=302)
        page = _page(provider, req, '<p class="err">Wrong secret.</p>')
        return page or HTMLResponse(_GONE, status_code=400)

    return get_login, post_login
