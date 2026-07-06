"""Log-time duplicate merges must pick the canonical by policy, not
"existing always wins".

Before the 2026-07-06 review (P1.2), consolidate_at_log unconditionally kept
the existing candidate and retired the just-logged lesson — even when the new
lesson was the more comprehensive write-up (lesson #1327). Log time must use
the same pick_canonical policy as the backlog path.

Also the first integration test of the full log_lesson→consolidate path
(candidates → judge → actor) against a real database.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.consolidation import config
from src.consolidation.orchestrator import consolidate_at_log

# Sparse direction: cosine ≈ 0.026 vs the constant [0.1]*1536 vectors other
# tests leave behind, so residue rows can never enter the candidate set.
_EMB = [1.0] + [0.0] * 1535
_EMB_STR = "[" + ",".join(str(x) for x in _EMB) + "]"


def _duplicate_judge():
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps({
        "relationship": "duplicate", "direction": None,
        "confidence": 0.95, "reasoning": "same advice",
    }))]
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _insert(conn, title, content):
    row = await conn.fetchrow(
        "INSERT INTO lessons (title, content, embedding, source_agent) "
        "VALUES ($1, $2, $3::vector, 'claude') RETURNING id",
        title, content, _EMB_STR,
    )
    return row["id"]


async def _cleanup(db_pool, *lesson_ids):
    ids = list(lesson_ids)
    await db_pool.execute(
        "DELETE FROM lesson_merges WHERE canonical_id = ANY($1) OR merged_id = ANY($1)", ids)
    await db_pool.execute(
        "DELETE FROM annotations WHERE entity_type='lesson' AND entity_id = ANY($1)", ids)
    await db_pool.execute("DELETE FROM lessons WHERE id = ANY($1)", ids)


@pytest.mark.asyncio
async def test_log_time_merge_keeps_more_comprehensive_new_lesson(db_pool, monkeypatch):
    monkeypatch.setattr(config, "ENABLED", True)
    async with db_pool.acquire() as conn:
        existing_id = await _insert(conn, "T_LTC_EXISTING", "thin note")
        new_id = await _insert(
            conn, "T_LTC_NEW", "comprehensive write-up with detail " * 20)
    try:
        summary = await consolidate_at_log(
            db_pool, _duplicate_judge(),
            new_lesson_id=new_id, new_title="T_LTC_NEW",
            new_content="comprehensive write-up with detail " * 20,
            new_embedding=_EMB, project_id=None, new_source_agent="claude",
        )
        assert summary["action_taken"] == "auto_merged"

        rows = {r["id"]: r for r in await db_pool.fetch(
            "SELECT id, retired_at FROM lessons WHERE id = ANY($1)",
            [existing_id, new_id])}
        assert rows[new_id]["retired_at"] is None, \
            "the comprehensive new lesson must survive"
        assert rows[existing_id]["retired_at"] is not None, \
            "the thin existing duplicate must be retired"
    finally:
        await _cleanup(db_pool, existing_id, new_id)


@pytest.mark.asyncio
async def test_log_time_merge_keeps_more_comprehensive_existing_lesson(db_pool, monkeypatch):
    monkeypatch.setattr(config, "ENABLED", True)
    async with db_pool.acquire() as conn:
        existing_id = await _insert(
            conn, "T_LTC_EXISTING2", "comprehensive write-up with detail " * 20)
        new_id = await _insert(conn, "T_LTC_NEW2", "thin note")
    try:
        summary = await consolidate_at_log(
            db_pool, _duplicate_judge(),
            new_lesson_id=new_id, new_title="T_LTC_NEW2",
            new_content="thin note",
            new_embedding=_EMB, project_id=None, new_source_agent="claude",
        )
        assert summary["action_taken"] == "auto_merged"

        rows = {r["id"]: r for r in await db_pool.fetch(
            "SELECT id, retired_at FROM lessons WHERE id = ANY($1)",
            [existing_id, new_id])}
        assert rows[existing_id]["retired_at"] is None
        assert rows[new_id]["retired_at"] is not None
    finally:
        await _cleanup(db_pool, existing_id, new_id)
