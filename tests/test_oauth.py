from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from finn_mcp import config
from finn_mcp.oauth import SCOPE, DualTokenVerifier, FinnAuthorizationServer, Store

PUBLIC = "https://finn.example.no"
RESOURCE = f"{PUBLIC}/mcp"
SECRET = "correct-horse-battery"
STATIC = "s" * 40


def _client(client_id: str = "claude-ai", redirect: str = "https://claude.ai/api/mcp/auth_callback") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="Claude",
        redirect_uris=[AnyUrl(redirect)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@pytest.fixture
def provider() -> FinnAuthorizationServer:
    return FinnAuthorizationServer(Store(":memory:"), PUBLIC, RESOURCE, SECRET)


async def _park(provider: FinnAuthorizationServer, client: OAuthClientInformationFull, resource: str | None = RESOURCE) -> str:
    await provider.register_client(client)
    _, challenge = _pkce()
    url = await provider.authorize(
        client,
        AuthorizationParams(
            state="xyz",
            scopes=[SCOPE],
            code_challenge=challenge,
            redirect_uri=client.redirect_uris[0],
            redirect_uri_provided_explicitly=True,
            resource=resource,
        ),
    )
    assert url.startswith(f"{PUBLIC}/login?req=")
    return parse_qs(urlparse(url).query)["req"][0]


# ---------- the login decision ----------

async def test_authorize_parks_the_request_and_sends_the_browser_to_login(provider):
    req = await _park(provider, _client())
    assert provider.store.get_pending(req) is not None


async def test_wrong_secret_mints_nothing(provider):
    req = await _park(provider, _client())
    assert provider.complete_login(req, "nope") is None
    # The request is still parked: a typo should not force the user to start over.
    assert provider.store.get_pending(req) is not None


async def test_login_mints_a_single_use_code_bound_to_the_client(provider):
    client = _client()
    req = await _park(provider, client)
    target = provider.complete_login(req, SECRET)
    assert target and target.startswith(str(client.redirect_uris[0]))
    q = parse_qs(urlparse(target).query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]
    # Parked request is consumed; a replayed login gets nothing.
    assert provider.complete_login(req, SECRET) is None
    # The code belongs to this client only.
    assert await provider.load_authorization_code(_client("someone-else"), code) is None
    record = await provider.load_authorization_code(client, code)
    assert record is not None and record.resource == RESOURCE


async def test_unknown_request_is_refused(provider):
    assert provider.complete_login("no-such-request", SECRET) is None


def test_login_secret_must_not_be_trivial():
    with pytest.raises(RuntimeError):
        FinnAuthorizationServer(Store(":memory:"), PUBLIC, RESOURCE, "short")


# ---------- tokens ----------

async def _tokens(provider, client):
    req = await _park(provider, client)
    code = parse_qs(urlparse(provider.complete_login(req, SECRET)).query)["code"][0]
    record = await provider.load_authorization_code(client, code)
    return code, await provider.exchange_authorization_code(client, record)


async def test_code_is_single_use(provider):
    client = _client()
    code, tokens = await _tokens(provider, client)
    assert tokens.token_type == "Bearer" and tokens.refresh_token
    assert await provider.load_authorization_code(client, code) is None


async def test_access_token_is_accepted_for_its_resource_only(provider):
    client = _client()
    _, tokens = await _tokens(provider, client)
    assert await provider.load_access_token(tokens.access_token) is not None
    # Same code path, token minted for another resource: refused.
    other = FinnAuthorizationServer(provider.store, PUBLIC, "https://other.example/mcp", SECRET)
    assert await other.load_access_token(tokens.access_token) is None


async def test_refresh_rotates(provider):
    client = _client()
    _, tokens = await _tokens(provider, client)
    old = await provider.load_refresh_token(client, tokens.refresh_token)
    assert old is not None
    fresh = await provider.exchange_refresh_token(client, old, [])
    assert fresh.access_token != tokens.access_token
    assert fresh.refresh_token != tokens.refresh_token
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None, "old refresh token must be dead"
    assert await provider.load_access_token(fresh.access_token) is not None


async def test_revocation(provider):
    client = _client()
    _, tokens = await _tokens(provider, client)
    record = await provider.load_access_token(tokens.access_token)
    await provider.revoke_token(record)
    assert await provider.load_access_token(tokens.access_token) is None


async def test_expired_rows_are_swept(provider, monkeypatch):
    client = _client()
    _, tokens = await _tokens(provider, client)
    monkeypatch.setattr("finn_mcp.oauth._now", lambda: 10**10)
    assert await provider.load_access_token(tokens.access_token) is None


# ---------- both kinds of bearer, one verifier ----------

async def test_verifier_accepts_static_and_issued_tokens(provider):
    verifier = DualTokenVerifier(STATIC, provider)
    static = await verifier.verify_token(STATIC)
    assert static is not None and static.client_id == "deployment"
    _, tokens = await _tokens(provider, _client())
    issued = await verifier.verify_token(tokens.access_token)
    assert issued is not None and issued.client_id == "claude-ai"
    assert await verifier.verify_token("garbage") is None
    assert await verifier.verify_token("") is None


async def test_verifier_without_oauth_still_takes_the_static_token():
    verifier = DualTokenVerifier(STATIC, None)
    assert await verifier.verify_token(STATIC) is not None
    assert await verifier.verify_token("x" * 40) is None


# ---------- the whole thing through HTTP ----------

@pytest.fixture
def app(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    monkeypatch.setenv("FINN_MCP_ACCESS_TOKEN", STATIC)
    # The DNS-rebinding guard must know the host the test client speaks to.
    monkeypatch.setenv("FINN_MCP_ALLOWED_HOST", "finn.example.no")
    monkeypatch.setattr(config, "PUBLIC_URL", PUBLIC)
    monkeypatch.setattr(config, "LOGIN_SECRET", SECRET)
    monkeypatch.setattr(config, "oauth_db_path", lambda: tmp_path / "oauth.sqlite")
    from finn_mcp.http_server import create_app
    from finn_mcp.server import mcp

    # The shared FastMCP instance keeps one session manager, and a manager
    # can only be run once; drop it so each test's lifespan starts a fresh one.
    mcp._session_manager = None
    # As a context manager the client runs the app's lifespan, which the
    # streamable HTTP session manager needs before it will serve /mcp.
    with TestClient(create_app(), base_url=PUBLIC) as client:
        yield client


def test_metadata_documents_are_public(app):
    rs = app.get("/.well-known/oauth-protected-resource/mcp")
    assert rs.status_code == 200
    assert rs.json()["authorization_servers"] == [f"{PUBLIC}/"] or rs.json()["authorization_servers"] == [PUBLIC]
    asm = app.get("/.well-known/oauth-authorization-server")
    assert asm.status_code == 200
    body = asm.json()
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["registration_endpoint"].endswith("/register")


def test_mcp_without_token_points_at_the_resource_metadata(app):
    r = app.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_static_token_still_opens_mcp(app):
    r = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={"Authorization": f"Bearer {STATIC}", "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200


def test_full_browser_flow_ends_with_a_working_token(app):
    reg = app.post("/register", json={
        "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]

    verifier, challenge = _pkce()
    auth = app.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "code_challenge": challenge,
        "code_challenge_method": "S256", "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "state": "s1", "resource": RESOURCE,
    }, follow_redirects=False)
    assert auth.status_code in (302, 307), auth.text
    login_url = auth.headers["location"]
    assert login_url.startswith(f"{PUBLIC}/login?req=")

    page = app.get(login_url)
    assert page.status_code == 200 and "Authorize <em>Claude</em>" in page.text
    req = parse_qs(urlparse(login_url).query)["req"][0]

    refused = app.post("/login", data={"req": req, "secret": "wrong"}, follow_redirects=False)
    assert refused.status_code == 401

    back = app.post("/login", data={"req": req, "secret": SECRET}, follow_redirects=False)
    assert back.status_code == 302
    q = parse_qs(urlparse(back.headers["location"]).query)
    assert q["state"] == ["s1"]

    tok = app.post("/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "code_verifier": verifier,
        "client_id": client_id, "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "resource": RESOURCE,
    })
    assert tok.status_code == 200, tok.text
    access = tok.json()["access_token"]

    ok = app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={"Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream"},
    )
    assert ok.status_code == 200, ok.text
