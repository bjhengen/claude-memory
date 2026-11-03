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

from src import approve_gate  # noqa: E402
from src.server import app, oauth_provider  # noqa: E402

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
    yield
    oauth_provider._auth_codes.clear()
    oauth_provider._pending_auth.clear()


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
    rid = _seed_pending(
        client_name="Evil <script>Corp</script>",
        redirect="https://attacker.example/callback",
        scopes=["read", "write"],
    )
    async with await _client() as client:
        resp = await client.get(f"/approve?id={rid}")
    body = resp.text
    assert "attacker.example" in body, "redirect_uri not shown"
    assert "read" in body and "write" in body, "scopes not shown"
    assert "Evil" in body
    assert "<script>Corp</script>" not in body, "client_name not HTML-escaped"
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
