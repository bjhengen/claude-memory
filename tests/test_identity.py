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
async def test_oauth_token_resolves_claude_family(db_pool):
    client_id = "client_test_oauth_claude"
    client_name = "claude-code-test"
    token = "oauth-test-token-eeee"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', $2, 'none', extract(epoch from NOW())::bigint, '{}'::jsonb)""",
        client_id, client_name,
    )
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
           VALUES ($1, $2, '[]'::jsonb, $3)""",
        token, client_id, 2**31 - 1,
    )
    try:
        identity = await resolve_identity(db_pool, token)
        assert identity is not None
        assert identity.family == "claude"
        assert identity.client_id == f"oauth:{client_id}"
        assert identity.source == "oauth"

        family_row = await db_pool.fetchrow(
            "SELECT family, inferred_from FROM oauth_client_family WHERE client_id = $1",
            client_id,
        )
        assert family_row["family"] == "claude"
        assert family_row["inferred_from"] == "client_name_prefix"
    finally:
        await db_pool.execute("DELETE FROM oauth_client_family WHERE client_id = $1", client_id)
        await db_pool.execute("DELETE FROM oauth_access_tokens WHERE token = $1", token)
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = $1", client_id)


@pytest.mark.asyncio
async def test_oauth_token_unknown_client_name(db_pool):
    client_id = "client_test_oauth_unknown"
    client_name = "some-random-app"
    token = "oauth-test-token-ffff"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', $2, 'none', extract(epoch from NOW())::bigint, '{}'::jsonb)""",
        client_id, client_name,
    )
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
           VALUES ($1, $2, '[]'::jsonb, $3)""",
        token, client_id, 2**31 - 1,
    )
    try:
        identity = await resolve_identity(db_pool, token)
        assert identity is not None
        assert identity.family == "unknown"
    finally:
        await db_pool.execute("DELETE FROM oauth_client_family WHERE client_id = $1", client_id)
        await db_pool.execute("DELETE FROM oauth_access_tokens WHERE token = $1", token)
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = $1", client_id)


@pytest.mark.asyncio
async def test_oauth_expired_token_does_not_resolve(db_pool):
    """Expired access tokens must NOT set identity, even if the row still exists."""
    client_id = "client_test_oauth_expired"
    token = "oauth-test-token-gggg"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', 'claude-code', 'none', extract(epoch from NOW())::bigint, '{}'::jsonb)""",
        client_id,
    )
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
           VALUES ($1, $2, '[]'::jsonb, $3)""",
        token, client_id, 1,  # expired
    )
    try:
        identity = await resolve_identity(db_pool, token)
        assert identity is None
    finally:
        await db_pool.execute("DELETE FROM oauth_access_tokens WHERE token = $1", token)
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = $1", client_id)


@pytest.mark.asyncio
async def test_load_access_token_sets_identity_via_apikey(db_pool):
    """An api_keys-issued bearer is accepted by load_access_token AND sets identity."""
    from src.auth import MemoryOAuthProvider

    raw = "auth-wire-apikey-hhhh"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, label, scopes)
           VALUES ($1, 'codex', 'auth-wire-test', ARRAY['read','write']) RETURNING id""",
        h,
    )
    key_id = row["id"]

    provider = MemoryOAuthProvider()
    provider.set_pool(db_pool)

    try:
        result = await provider.load_access_token(raw)
        assert result is not None
        assert result.client_id == f"apikey:{key_id}"

        identity = get_identity()
        assert identity is not None
        assert identity.family == "codex"
        assert identity.source == "apikey"
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key_id)
