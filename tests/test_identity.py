"""Identity resolver branch tests."""

import hashlib
import os

import pytest

from src.identity import (
    Identity, resolve_identity, get_identity, set_identity, reset_identity,
)


@pytest.fixture(autouse=True)
def _reset_between_tests():
    reset_identity()
    yield
    reset_identity()


@pytest.mark.asyncio
async def test_legacy_api_key_resolves_to_claude(db_pool, monkeypatch):
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", "legacy-secret-xyz")

    identity = await resolve_identity(db_pool, "legacy-secret-xyz")

    assert identity is not None
    assert identity.family == "claude"
    assert identity.client_id == "legacy-api-key"
    assert identity.scopes == ["read", "write"]
    assert identity.source == "legacy"


@pytest.mark.asyncio
async def test_unknown_bearer_returns_none(db_pool):
    identity = await resolve_identity(db_pool, "definitely-not-a-real-token")
    assert identity is None


def test_set_and_get_and_reset():
    assert get_identity() is None
    set_identity(Identity(family="codex", client_id="x", scopes=["read"], source="apikey"))
    assert get_identity().family == "codex"
    reset_identity()
    assert get_identity() is None


@pytest.mark.asyncio
async def test_api_keys_hash_match(db_pool):
    raw = "test-bearer-aaaaaaaaaaaa"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, client_name, label, scopes)
           VALUES ($1, 'codex', 'codex-cli', 'test row', ARRAY['read','write'])
           RETURNING id""",
        h,
    )
    key_id = row["id"]
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is not None
        assert identity.family == "codex"
        assert identity.client_id == f"apikey:{key_id}"
        assert identity.scopes == ["read", "write"]
        assert identity.source == "apikey"

        last_seen = await db_pool.fetchval(
            "SELECT last_seen_at FROM api_keys WHERE id = $1", key_id,
        )
        assert last_seen is not None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key_id)


@pytest.mark.asyncio
async def test_api_keys_revoked_does_not_match(db_pool):
    raw = "test-bearer-revoked-bbbb"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, scopes, revoked_at)
           VALUES ($1, 'codex', ARRAY['read','write'], NOW()) RETURNING id""",
        h,
    )
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_api_keys_admin_scope_preserved(db_pool):
    raw = "test-bearer-admin-cccc"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, scopes)
           VALUES ($1, 'claude', ARRAY['read','write','admin']) RETURNING id""",
        h,
    )
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is not None
        assert "admin" in identity.scopes
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_api_keys_wins_over_legacy(db_pool, monkeypatch):
    """A bearer that matches BOTH legacy API_KEY and an api_keys row resolves via api_keys."""
    raw = "double-match-bearer-dddd"
    h = hashlib.sha256(raw.encode()).hexdigest()
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", raw)
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, label, scopes)
           VALUES ($1, 'codex', 'overlap', ARRAY['read','write']) RETURNING id""",
        h,
    )
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is not None
        assert identity.source == "apikey"
        assert identity.family == "codex"
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", row["id"])
