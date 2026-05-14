"""Identity resolver branch tests."""

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
