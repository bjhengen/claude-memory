"""Per-client last-seen tracking and the non-admin get_client_health view.

Review 2026-07-06 (P1.5): api_keys.last_seen_at existed but OAuth clients had
no equivalent and nothing surfaced staleness, so a launch context going dark
(work-laptop CLI registration vanishing May–Jul 2026) went unnoticed for
weeks. get_client_health must be callable WITHOUT admin scope and must never
expose secrets.
"""

import hashlib
import json
import time
from unittest.mock import MagicMock

import pytest

from src.identity import resolve_identity, reset_identity
from src.server import AppContext
from src.tools.admin import get_client_health


@pytest.fixture(autouse=True)
def _clean_identity():
    reset_identity()
    yield
    reset_identity()


def _ctx(db_pool):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )
    return ctx


@pytest.mark.asyncio
async def test_oauth_resolution_stamps_client_last_seen(db_pool):
    token = "tch-oauth-token"
    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_name, raw_data)
           VALUES ('tch-client', 'claude-ai (test)', '{}')
           ON CONFLICT (client_id) DO UPDATE SET last_seen_at = NULL""")
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, expires_at)
           VALUES ($1, 'tch-client', $2)
           ON CONFLICT (token) DO NOTHING""",
        token, int(time.time()) + 3600)
    try:
        identity = await resolve_identity(db_pool, token)
        assert identity is not None and identity.source == "oauth"

        seen = await db_pool.fetchval(
            "SELECT last_seen_at FROM oauth_clients WHERE client_id = 'tch-client'")
        assert seen is not None, "OAuth resolution must stamp oauth_clients.last_seen_at"
    finally:
        await db_pool.execute("DELETE FROM oauth_access_tokens WHERE token = $1", token)
        await db_pool.execute("DELETE FROM oauth_client_family WHERE client_id = 'tch-client'")
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = 'tch-client'")


@pytest.mark.asyncio
async def test_client_health_reports_staleness_without_admin(db_pool):
    fresh_hash = hashlib.sha256(b"tch-fresh-key").hexdigest()
    stale_hash = hashlib.sha256(b"tch-stale-key").hexdigest()
    fresh = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, client_name, label, scopes, last_seen_at)
           VALUES ($1, 'claude', 'cli', 'tch-fresh', ARRAY['read','write'], NOW())
           RETURNING id""", fresh_hash)
    stale = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, client_name, label, scopes, last_seen_at)
           VALUES ($1, 'claude', 'cli', 'tch-stale', ARRAY['read','write'], NOW() - INTERVAL '10 days')
           RETURNING id""", stale_hash)
    try:
        # No admin identity set — must NOT raise PermissionError
        payload = json.loads(await get_client_health(ctx=_ctx(db_pool)))
        by_name = {c["name"]: c for c in payload["clients"]}

        assert by_name["tch-fresh"]["stale"] is False
        assert by_name["tch-fresh"]["days_since_seen"] == 0
        assert by_name["tch-stale"]["stale"] is True
        assert by_name["tch-stale"]["days_since_seen"] == 10

        # No secrets anywhere in the payload
        assert fresh_hash not in json.dumps(payload)
        assert stale_hash not in json.dumps(payload)
    finally:
        await db_pool.execute(
            "DELETE FROM api_keys WHERE id = ANY($1)", [fresh["id"], stale["id"]])


@pytest.mark.asyncio
async def test_client_health_excludes_revoked_keys(db_pool):
    revoked_hash = hashlib.sha256(b"tch-revoked-key").hexdigest()
    revoked = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, client_name, label, scopes,
                                 last_seen_at, revoked_at)
           VALUES ($1, 'claude', 'cli', 'tch-revoked', ARRAY['read','write'], NOW(), NOW())
           RETURNING id""", revoked_hash)
    try:
        payload = json.loads(await get_client_health(ctx=_ctx(db_pool)))
        names = [c["name"] for c in payload["clients"]]
        assert "tch-revoked" not in names
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", revoked["id"])
