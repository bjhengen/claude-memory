"""P2 feedback/hardening behaviors: downvote retirement flag, stats fields,
idempotent auto-merge.

Review 2026-07-06: votes no longer scale ranking (v9), so a downvote's
honest job is flagging the lesson for human retirement review; stats must
surface that count plus conflict-queue aging. execute_auto_merge must be
retry/concurrency-safe (previously a plain INSERT raised UniqueViolation).
"""

import json
from unittest.mock import MagicMock

import pytest

from src.consolidation.actor import execute_auto_merge
from src.consolidation.judge import JudgeVerdict
from src.server import AppContext
from src.tools.consolidation import _compute_stats
from src.tools.lessons import rate_lesson

_EMB_STR = "[" + ",".join(["0.1"] * 1536) + "]"


def _ctx(db_pool):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )
    return ctx


@pytest.mark.asyncio
async def test_downvote_flags_lesson_for_review(db_pool):
    row = await db_pool.fetchrow(
        "INSERT INTO lessons (title, content) VALUES ('T_P2F_down', 'x') RETURNING id")
    lesson_id = row["id"]
    try:
        result = json.loads(await rate_lesson(
            lesson_id=lesson_id, rating="down", ctx=_ctx(db_pool)))
        assert result["flagged_for_review"] is True

        notes = await db_pool.fetch(
            "SELECT note FROM annotations WHERE entity_type='lesson' AND entity_id=$1",
            lesson_id)
        assert any("retirement review" in n["note"] for n in notes)
    finally:
        await db_pool.execute(
            "DELETE FROM annotations WHERE entity_type='lesson' AND entity_id=$1", lesson_id)
        await db_pool.execute("DELETE FROM lessons WHERE id=$1", lesson_id)


@pytest.mark.asyncio
async def test_stats_include_flagged_and_conflict_age(db_pool):
    stats = await _compute_stats(db_pool, 7)
    assert "lessons_flagged_for_review" in stats
    assert "oldest_pending_conflict_days" in stats


@pytest.mark.asyncio
async def test_auto_merge_is_idempotent_on_retry(db_pool):
    async with db_pool.acquire() as conn:
        a = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding) "
            "VALUES ('T_P2F_canon', 'x', $1::vector) RETURNING id", _EMB_STR)
        b = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding) "
            "VALUES ('T_P2F_merged', 'y', $1::vector) RETURNING id", _EMB_STR)
    canonical_id, merged_id = a["id"], b["id"]
    verdict = JudgeVerdict("duplicate", None, 0.95, "same")
    try:
        first = await execute_auto_merge(
            db_pool, new_lesson_id=merged_id, canonical_id=canonical_id,
            verdict=verdict, cosine=0.99, judge_model="test")
        second = await execute_auto_merge(
            db_pool, new_lesson_id=merged_id, canonical_id=canonical_id,
            verdict=verdict, cosine=0.99, judge_model="test")
        assert second == first  # same audit row, no UniqueViolation, no double transfer
    finally:
        ids = [canonical_id, merged_id]
        await db_pool.execute(
            "DELETE FROM lesson_merges WHERE canonical_id = ANY($1) OR merged_id = ANY($1)", ids)
        await db_pool.execute(
            "DELETE FROM annotations WHERE entity_type='lesson' AND entity_id = ANY($1)", ids)
        await db_pool.execute("DELETE FROM lessons WHERE id = ANY($1)", ids)
