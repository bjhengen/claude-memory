"""Tests for stamp(), assert_can_write(), require_admin()."""

import pytest

from src.identity import (
    Identity, set_identity, reset_identity,
    stamp, assert_can_write, require_admin,
)


@pytest.fixture(autouse=True)
def _reset_between():
    reset_identity()
    yield
    reset_identity()


def test_stamp_returns_current_identity():
    set_identity(Identity(
        family="codex", client_id="apikey:42",
        scopes=["read", "write"], source="apikey",
    ))
    family, client_id = stamp()
    assert family == "codex"
    assert client_id == "apikey:42"


def test_stamp_defaults_when_unauth():
    family, client_id = stamp()
    assert family == "claude"
    assert client_id is None


@pytest.mark.asyncio
async def test_assert_can_write_allows_own_row(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b own', 'c', 'codex') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        await assert_can_write(db_pool, "lessons", row["id"])
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_assert_can_write_blocks_foreign_row(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b foreign', 'c', 'claude') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        with pytest.raises(PermissionError) as exc:
            await assert_can_write(db_pool, "lessons", row["id"])
        assert "codex" in str(exc.value)
        assert "claude" in str(exc.value)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_assert_can_write_shared_metadata_always_allows(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO projects (name, source_agent)
           VALUES ('rule-b-shared', 'claude') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        await assert_can_write(db_pool, "projects", row["id"])
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_assert_can_write_admin_bypass(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b admin', 'c', 'claude') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write", "admin"], source="apikey",
        ))
        await assert_can_write(db_pool, "lessons", row["id"])
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", row["id"])


def test_require_admin_blocks_non_admin():
    set_identity(Identity(
        family="codex", client_id="apikey:99",
        scopes=["read", "write"], source="apikey",
    ))
    with pytest.raises(PermissionError):
        require_admin()


def test_require_admin_passes_when_admin():
    set_identity(Identity(
        family="codex", client_id="apikey:99",
        scopes=["read", "write", "admin"], source="apikey",
    ))
    require_admin()
