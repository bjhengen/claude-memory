"""read_journal must return previews by default, full text only on request.

Review 2026-07-06 (P1.3): read_journal returned full content for every entry
— limit=20 produced an 82K-char payload that blew past MCP client token
budgets. Default to 500-char previews (matching search's truncation) with a
full_content opt-in.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.server import AppContext
from src.tools.journal import read_journal


def _ctx(db_pool):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )
    return ctx


LONG = "reflection sentence with some substance to it. " * 60  # ~2.8K chars


async def _seed(db_pool, name):
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ($1) RETURNING id", name)
    entry = await db_pool.fetchrow(
        "INSERT INTO journal (content, project_id) VALUES ($1, $2) RETURNING id",
        LONG, proj["id"])
    return proj["id"], entry["id"]


async def _cleanup(db_pool, project_id, entry_id):
    await db_pool.execute("DELETE FROM journal WHERE id = $1", entry_id)
    await db_pool.execute("DELETE FROM projects WHERE id = $1", project_id)


@pytest.mark.asyncio
async def test_read_journal_truncates_by_default(db_pool):
    pid, eid = await _seed(db_pool, "rj-preview-default")
    try:
        result = await read_journal(project="rj-preview-default", ctx=_ctx(db_pool))
        entries = json.loads(result)["entries"]
        assert len(entries) == 1
        assert len(entries[0]["content"]) <= 510
        assert entries[0]["truncated"] is True
    finally:
        await _cleanup(db_pool, pid, eid)


@pytest.mark.asyncio
async def test_read_journal_full_content_opt_in(db_pool):
    pid, eid = await _seed(db_pool, "rj-preview-full")
    try:
        result = await read_journal(
            project="rj-preview-full", full_content=True, ctx=_ctx(db_pool))
        entries = json.loads(result)["entries"]
        assert entries[0]["content"] == LONG
        assert entries[0]["truncated"] is False
    finally:
        await _cleanup(db_pool, pid, eid)


@pytest.mark.asyncio
async def test_read_journal_short_entry_not_marked_truncated(db_pool):
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('rj-preview-short') RETURNING id")
    entry = await db_pool.fetchrow(
        "INSERT INTO journal (content, project_id) VALUES ('brief note', $1) RETURNING id",
        proj["id"])
    try:
        result = await read_journal(project="rj-preview-short", ctx=_ctx(db_pool))
        entries = json.loads(result)["entries"]
        assert entries[0]["content"] == "brief note"
        assert entries[0]["truncated"] is False
    finally:
        await _cleanup(db_pool, proj["id"], entry["id"])
