"""Tests for the auto-merge DB mutation path."""

import pytest

from src.consolidation.actor import execute_auto_merge
from src.consolidation.judge import JudgeVerdict


async def _insert_lesson(conn, title, content, tags=None, severity="tip",
                         upvotes=0, downvotes=0):
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    row = await conn.fetchrow(
        """
        INSERT INTO lessons (title, content, embedding, tags, severity, upvotes, downvotes)
        VALUES ($1, $2, $3::vector, $4, $5, $6, $7)
        RETURNING id
        """,
        title, content, emb, tags or [], severity, upvotes, downvotes
    )
    return row["id"]


@pytest.mark.asyncio
async def test_auto_merge_retires_new_and_transfers_counters(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_MRG_%'")
        canonical_id = await _insert_lesson(conn, "T_MRG_CANON", "existing",
                                            upvotes=3, downvotes=1, tags=["alpha"],
                                            severity="tip")
        new_id = await _insert_lesson(conn, "T_MRG_NEW", "same thing",
                                      upvotes=0, downvotes=0, tags=["beta"],
                                      severity="critical")

    verdict = JudgeVerdict("duplicate", None, 0.94, "dupes")
    merge_id = await execute_auto_merge(
        db_pool, new_lesson_id=new_id, canonical_id=canonical_id,
        verdict=verdict, cosine=0.91, judge_model="claude-haiku-4-5-20251001",
    )

    async with db_pool.acquire() as conn:
        new_row = await conn.fetchrow("SELECT retired_at, retired_reason FROM lessons WHERE id=$1", new_id)
        canon_row = await conn.fetchrow(
            "SELECT upvotes, downvotes, tags, severity FROM lessons WHERE id=$1",
            canonical_id,
        )
        audit = await conn.fetchrow("SELECT * FROM lesson_merges WHERE id=$1", merge_id)

    assert new_row["retired_at"] is not None
    assert "merged into" in new_row["retired_reason"]
    assert canon_row["upvotes"] == 3  # new had 0 to transfer
    assert "alpha" in canon_row["tags"] and "beta" in canon_row["tags"]
    assert canon_row["severity"] == "critical"  # escalated from "tip"
    assert audit["action"] == "merged"
    assert audit["auto_decided"] is True
    assert audit["canonical_id"] == canonical_id
    assert audit["merged_id"] == new_id
