"""Tests for supersede, enqueue, and flag-conflict DB paths."""

import pytest

from src.consolidation.actor import (
    execute_auto_supersede, execute_enqueue, execute_flag_conflict,
)
from src.consolidation.judge import JudgeVerdict


async def _insert_lesson(conn, title, content, upvotes=0, downvotes=0):
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    row = await conn.fetchrow(
        "INSERT INTO lessons (title, content, embedding, upvotes, downvotes) "
        "VALUES ($1,$2,$3::vector,$4,$5) RETURNING id",
        title, content, emb, upvotes, downvotes,
    )
    return row["id"]


@pytest.mark.asyncio
async def test_auto_supersede_new_to_existing_retires_existing(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_SUP_%'")
        existing_id = await _insert_lesson(conn, "T_SUP_OLD", "old", upvotes=2)
        new_id = await _insert_lesson(conn, "T_SUP_NEW", "new")

    verdict = JudgeVerdict("supersedes", "new→existing", 0.97, "replaces")
    merge_id = await execute_auto_supersede(
        db_pool, new_lesson_id=new_id, existing_lesson_id=existing_id,
        verdict=verdict, cosine=0.88, judge_model="claude-haiku-4-5-20251001",
    )

    async with db_pool.acquire() as conn:
        existing_row = await conn.fetchrow("SELECT retired_at FROM lessons WHERE id=$1", existing_id)
        new_row = await conn.fetchrow("SELECT retired_at, upvotes FROM lessons WHERE id=$1", new_id)
        audit = await conn.fetchrow("SELECT * FROM lesson_merges WHERE id=$1", merge_id)

    assert existing_row["retired_at"] is not None  # existing retired
    assert new_row["retired_at"] is None           # new stays live
    assert new_row["upvotes"] == 2                 # counters moved from existing
    assert audit["canonical_id"] == new_id
    assert audit["merged_id"] == existing_id
    assert audit["action"] == "superseded"


@pytest.mark.asyncio
async def test_enqueue_creates_queue_row_and_annotations(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_Q_%'")
        cand_id = await _insert_lesson(conn, "T_Q_CAND", "old")
        new_id = await _insert_lesson(conn, "T_Q_NEW", "new")

    verdict = JudgeVerdict("duplicate", None, 0.72, "possibly dup")
    queue_id = await execute_enqueue(
        db_pool, new_lesson_id=new_id, candidate_lesson_id=cand_id,
        verdict=verdict, cosine=0.82, judge_model="claude-haiku-4-5-20251001",
        proposed_action="merged",
    )

    async with db_pool.acquire() as conn:
        q = await conn.fetchrow("SELECT * FROM consolidation_queue WHERE id=$1", queue_id)
        new_ann = await conn.fetchrow(
            "SELECT note FROM annotations WHERE entity_type='lesson' AND entity_id=$1", new_id)
        cand_ann = await conn.fetchrow(
            "SELECT note FROM annotations WHERE entity_type='lesson' AND entity_id=$1", cand_id)

    assert q["decision"] is None
    assert q["new_lesson_id"] == new_id
    assert q["candidate_lesson_id"] == cand_id
    assert "Consolidation pending" in new_ann["note"]
    assert "Consolidation pending" in cand_ann["note"]


@pytest.mark.asyncio
async def test_flag_conflict_creates_conflict_row_with_canonical_ordering(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_CF_%'")
        a_id = await _insert_lesson(conn, "T_CF_A", "x")
        b_id = await _insert_lesson(conn, "T_CF_B", "y")

    verdict = JudgeVerdict("contradicts", None, 0.80, "opposite advice")
    conflict_id = await execute_flag_conflict(
        db_pool, new_lesson_id=b_id, candidate_lesson_id=a_id,
        verdict=verdict, cosine=0.81, judge_model="claude-haiku-4-5-20251001",
    )

    async with db_pool.acquire() as conn:
        c = await conn.fetchrow("SELECT * FROM lesson_conflicts WHERE id=$1", conflict_id)

    # Canonical ordering: lesson_a_id < lesson_b_id regardless of call-site order
    assert c["lesson_a_id"] == min(a_id, b_id)
    assert c["lesson_b_id"] == max(a_id, b_id)
    assert c["resolved_at"] is None
