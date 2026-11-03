"""Integration tests for the gated OAuth `/approve` route.

Drives the real Starlette route through httpx.ASGITransport. The `/approve`
flow is in-memory only (no DB): we seed a pending authorization directly on
the provider and assert on what the POST does.

The headline regression is test_post_without_credentials_issues_no_code — the
exact exploit the sibling session found: an unauthenticated POST must not mint
an auth code.
"""

import os

import httpx
import pytest

# config reads env at import time; set before importing the server.
os.environ.setdefault("DATABASE_URL", "postgresql://claude:claude@localhost:5434/claude_memory_test")
os.environ.setdefault("OPENAI_API_KEY", "sk-dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-dummy")

from mcp.server.auth.provider import AuthorizationParams  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402

from starlette.requests import Request  # noqa: E402

from src import approve_gate  # noqa: E402
from src.server import app, oauth_provider, _approve_rate_limiter, _client_ip  # noqa: E402


def _make_request(headers: dict, client) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": client,
    }
    return Request(scope)


def test_client_ip_trusts_x_real_ip_from_loopback_peer():
    req = _make_request({"x-real-ip": "203.0.113.7"}, ("127.0.0.1", 5000))
    assert _client_ip(req) == "203.0.113.7"


def test_client_ip_trusts_x_real_ip_from_docker_bridge_gateway():
    # Published 127.0.0.1 port => the container sees the private bridge gateway
    # as the peer, not loopback. X-Real-IP (set by nginx) must still be trusted,
    # else every client collapses into one rate-limit bucket.
    req = _make_request({"x-real-ip": "203.0.113.7"}, ("172.18.0.1", 5000))
    assert _client_ip(req) == "203.0.113.7"


def test_client_ip_ignores_x_real_ip_from_public_peer():
    # A public peer means the app was reached without nginx; a spoofed X-Real-IP
    # there must be ignored (rate-limit evasion defense). 8.8.8.8 is genuinely
    # public (unlike the RFC 5737 doc ranges, which Python treats as private).
    req = _make_request({"x-real-ip": "1.2.3.4"}, ("8.8.8.8", 5000))
    assert _client_ip(req) == "8.8.8.8"


def test_client_ip_falls_back_to_peer_without_header():
    req = _make_request({}, ("172.18.0.1", 5000))
    assert _client_ip(req) == "172.18.0.1"

PASSPHRASE = "open-sesame-correct-horse"
COOKIE_SECRET = "z" * 64
BASE_URL = "http://localhost:8003"


def _seed_pending(
    request_id: str = "test-req-id",
    client_name: str | None = None,
    redirect: str = "https://example.com/callback",
    scopes: list[str] | None = None,
) -> str:
    """Register an in-memory pending authorization and return its id."""
    client = OAuthClientInformationFull(
        client_id="client_test",
        client_name=client_name,
        redirect_uris=[redirect],
    )
    params = AuthorizationParams(
        state="xyz",
        scopes=scopes if scopes is not None else ["read", "write"],
        code_challenge="challenge",
        redirect_uri=redirect,
        redirect_uri_provided_explicitly=True,
    )
    import time
    oauth_provider._pending_auth[request_id] = {
        "client": client,
        "params": params,
        "created_at": time.time(),
    }
    return request_id


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch):
    monkeypatch.setenv("APPROVE_PASSPHRASE", PASSPHRASE)
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", COOKIE_SECRET)
    oauth_provider._auth_codes.clear()
    oauth_provider._pending_auth.clear()
    _approve_rate_limiter._hits.clear()
    yield
    oauth_provider._auth_codes.clear()
    oauth_provider._pending_auth.clear()
    _approve_rate_limiter._hits.clear()


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_consent_page_shows_what_is_being_approved_escaped():
    """Finding 1: the human must see the client, redirect, and scopes they are
    authorizing — HTML-escaped — and the page must be unframeable (clickjacking)."""
    # Client requests read-only, but the grant is always full read+write; the
    # page must state the real grant, not echo the client's requested scopes.
    rid = _seed_pending(
        client_name="Evil <script>Corp</script>",
        redirect="https://attacker.example/callback",
        scopes=["read"],
    )
    async with await _client() as client:
        resp = await client.get(f"/approve?id={rid}")
    body = resp.text.lower()
    assert "attacker.example" in body, "redirect_uri not shown"
    assert "non-admin" in body, "honest grant statement (full read/write) not shown"
    assert "write" in body, "grant does not mention write access"
    assert "unverified" in body, "client name not labeled as self-reported/unverified"
    assert "evil" in body
    assert "<script>corp</script>" not in body, "client_name not HTML-escaped"
    assert resp.headers.get("x-frame-options") == "DENY", "approve page is frameable"


@pytest.mark.asyncio
async def test_post_without_credentials_issues_no_code():
    """THE regression: no passphrase and no cookie must not mint an auth code."""
    rid = _seed_pending()
    async with await _client() as client:
        resp = await client.post("/approve", data={"id": rid},
                                 headers={"X-Real-IP": "203.0.113.1"})
    assert resp.status_code in (401, 403), resp.text
    assert resp.status_code != 302
    assert oauth_provider._auth_codes == {}, "an auth code was issued without credentials"


@pytest.mark.asyncio
async def test_post_with_correct_passphrase_issues_code_and_sets_cookie():
    rid = _seed_pending()
    async with await _client() as client:
        resp = await client.post(
            "/approve",
            data={"id": rid, "passphrase": PASSPHRASE},
            headers={"X-Real-IP": "203.0.113.2"},
        )
    assert resp.status_code == 302, resp.text
    assert "code=" in resp.headers["location"]
    set_cookie = resp.headers.get("set-cookie", "")
    assert approve_gate.COOKIE_NAME in set_cookie, set_cookie
    assert len(oauth_provider._auth_codes) == 1


@pytest.mark.asyncio
async def test_post_with_valid_cookie_skips_passphrase():
    rid = _seed_pending()
    token = approve_gate.make_cookie()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        follow_redirects=False,
        cookies={approve_gate.COOKIE_NAME: token},
    ) as client:
        resp = await client.post(
            "/approve",
            data={"id": rid},  # no passphrase
            headers={"X-Real-IP": "203.0.113.3"},
        )
    assert resp.status_code == 302, resp.text
    assert "code=" in resp.headers["location"]


@pytest.mark.asyncio
async def test_authorize_purges_expired_pending_entries():
    """authorize() must drop stale pending requests so the dict can't grow
    unbounded from /authorize spam."""
    import time
    from src.auth import MemoryOAuthProvider

    provider = MemoryOAuthProvider()
    provider._pending_auth["stale"] = {
        "client": None, "params": None, "created_at": time.time() - 601,
    }
    client = OAuthClientInformationFull(
        client_id="c", redirect_uris=["https://example.com/callback"],
    )
    params = AuthorizationParams(
        state="s", scopes=["read"], code_challenge="x",
        redirect_uri="https://example.com/callback",
        redirect_uri_provided_explicitly=True,
    )
    await provider.authorize(client, params)
    assert "stale" not in provider._pending_auth


@pytest.mark.asyncio
async def test_valid_cookie_bypasses_rate_limit():
    """A remembered browser must authorize even when its IP is rate-limited —
    an attacker's failed attempts can't lock out the legit user."""
    ip = "203.0.113.50"
    async with await _client() as client:
        for _ in range(10):  # trip the limiter for this IP
            rid = _seed_pending()
            await client.post("/approve", data={"id": rid, "passphrase": "wrong"},
                              headers={"X-Real-IP": ip})
        # sanity: a passphrase attempt from that IP is now blocked
        rid = _seed_pending()
        blocked = await client.post("/approve", data={"id": rid, "passphrase": PASSPHRASE},
                                    headers={"X-Real-IP": ip})
    assert blocked.status_code == 429

    rid = _seed_pending()
    token = approve_gate.make_cookie()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
        follow_redirects=False, cookies={approve_gate.COOKIE_NAME: token},
    ) as client:
        resp = await client.post("/approve", data={"id": rid}, headers={"X-Real-IP": ip})
    assert resp.status_code == 302, resp.text


@pytest.mark.asyncio
async def test_repeated_wrong_passphrase_is_rate_limited():
    ip = "203.0.113.99"
    async with await _client() as client:
        last = None
        for _ in range(12):
            rid = _seed_pending()
            last = await client.post(
                "/approve",
                data={"id": rid, "passphrase": "wrong"},
                headers={"X-Real-IP": ip},
            )
    assert last.status_code == 429, last.status_code
    assert oauth_provider._auth_codes == {}
