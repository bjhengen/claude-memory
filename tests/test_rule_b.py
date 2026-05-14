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


from unittest.mock import MagicMock
import json as _json

from src.server import AppContext


def _ctx(db_pool, mock_openai=None, mock_anthropic=None):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool,
        openai=mock_openai or MagicMock(),
        anthropic=mock_anthropic or MagicMock(),
    )
    return ctx


def _codex():
    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))


@pytest.mark.asyncio
async def test_log_lesson_stamps_codex(db_pool, mock_openai, mock_anthropic):
    from src.tools.lessons import log_lesson
    _codex()
    await db_pool.execute("DELETE FROM lessons WHERE title = $1", "v6-stamp-lesson")
    result = await log_lesson(
        title="v6-stamp-lesson",
        content="stamped by codex",
        ctx=_ctx(db_pool, mock_openai, mock_anthropic),
    )
    payload = _json.loads(result)
    lesson_id = payload["lesson_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent, source_client_id FROM lessons WHERE id = $1",
            lesson_id,
        )
        assert row["source_agent"] == "codex"
        assert row["source_client_id"] == "apikey:7"
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_log_pattern_stamps_codex(db_pool, mock_openai):
    from src.tools.lessons import log_pattern
    _codex()
    await db_pool.execute("DELETE FROM patterns WHERE name = $1", "v6-stamp-pattern")
    result = await log_pattern(
        name="v6-stamp-pattern",
        problem="p", solution="s",
        ctx=_ctx(db_pool, mock_openai),
    )
    payload = _json.loads(result)
    pat_id = payload["pattern_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM patterns WHERE id = $1", pat_id,
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM patterns WHERE id = $1", pat_id)


@pytest.mark.asyncio
async def test_write_journal_stamps_codex(db_pool, mock_openai):
    from src.tools.journal import write_journal
    _codex()
    result = await write_journal(
        content="codex first journal", tags=["v6"],
        ctx=_ctx(db_pool, mock_openai),
    )
    payload = _json.loads(result)
    eid = payload["entry_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent, source_client_id FROM journal WHERE id = $1", eid,
        )
        assert row["source_agent"] == "codex"
        assert row["source_client_id"] == "apikey:7"
    finally:
        await db_pool.execute("DELETE FROM journal WHERE id = $1", eid)


@pytest.mark.asyncio
async def test_rate_lesson_cross_agent_allowed(db_pool):
    """codex can rate a claude lesson (votes aren't ownership)."""
    from src.tools.lessons import rate_lesson
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-rate-x-agent', 'c', 'claude') RETURNING id""",
    )
    lesson_id = row["id"]
    _codex()
    try:
        result = await rate_lesson(
            lesson_id=lesson_id, rating="up", ctx=_ctx(db_pool),
        )
        payload = _json.loads(result)
        assert payload.get("success") is True
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)
