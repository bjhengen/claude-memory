"""Approving a queue entry must never act on an already-retired lesson.

A pending entry is judged at enqueue time but decided later — days or months
later, in practice. Either lesson can be retired in the interval by another
merge, a manual retire_lesson, or an approved neighbouring queue entry.

The live incident (queue 50, 2026-09-12): the NEW lesson #1429 was retired
2026-07-25, two weeks after enqueue, while its candidate #1358 stayed active.
Approving would have retired the live survivor in favour of a tombstone,
leaving a supersede chain with no readable endpoint.
"""

import pytest

from src.consolidation.actor import (
    execute_auto_merge,
    execute_auto_supersede,
)
from src.consolidation.judge import JudgeVerdict


async def _mk_lesson(pool, title, content="body", retired=False):
    row = await pool.fetchrow(
        "INSERT INTO lessons (title, content, severity) VALUES ($1, $2, 'tip') "
        "RETURNING id",
        title, content,
    )
    if retired:
        await pool.execute(
            "UPDATE lessons SET retired_at=NOW(), retired_reason='test fixture' "
            "WHERE id=$1",
            row["id"],
        )
    return row["id"]


async def _cleanup(pool, ids):
    await pool.execute("DELETE FROM lesson_merges WHERE canonical_id = ANY($1) "
                       "OR merged_id = ANY($1)", ids)
    await pool.execute("DELETE FROM lessons WHERE id = ANY($1)", ids)


@pytest.mark.asyncio
async def test_supersede_refuses_when_the_surviving_lesson_is_retired(db_pool):
    """The queue-50 shape: new→existing where NEW (the survivor) is retired.

    execute_auto_supersede retires `existing` and makes `new` canonical. If
    `new` is already retired, the operation destroys the only live lesson of
    the pair.
    """
    new_id = await _mk_lesson(db_pool, "retired survivor", retired=True)
    existing_id = await _mk_lesson(db_pool, "live candidate")
    verdict = JudgeVerdict("supersedes", "new→existing", 0.92, "test")

    try:
        with pytest.raises(ValueError, match="retired"):
            await execute_auto_supersede(
                db_pool, new_lesson_id=new_id, existing_lesson_id=existing_id,
                verdict=verdict, cosine=0.9, judge_model="test",
            )

        # The live lesson must be untouched.
        still_live = await db_pool.fetchval(
            "SELECT retired_at FROM lessons WHERE id=$1", existing_id,
        )
        assert still_live is None, "the live lesson was retired by a refused operation"
    finally:
        await _cleanup(db_pool, [new_id, existing_id])


@pytest.mark.asyncio
async def test_merge_refuses_when_the_canonical_lesson_is_retired(db_pool):
    """Merging INTO a retired canonical buries the surviving content."""
    new_id = await _mk_lesson(db_pool, "live new")
    canonical_id = await _mk_lesson(db_pool, "retired canonical", retired=True)
    verdict = JudgeVerdict("duplicate", None, 0.95, "test")

    try:
        with pytest.raises(ValueError, match="retired"):
            await execute_auto_merge(
                db_pool, new_lesson_id=new_id, canonical_id=canonical_id,
                verdict=verdict, cosine=0.95, judge_model="test",
            )

        still_live = await db_pool.fetchval(
            "SELECT retired_at FROM lessons WHERE id=$1", new_id,
        )
        assert still_live is None, "the live lesson was retired into a tombstone"
    finally:
        await _cleanup(db_pool, [new_id, canonical_id])


@pytest.mark.asyncio
async def test_merge_of_an_already_merged_lesson_still_returns_its_audit_row(db_pool):
    """Regression guard: the new guard must not break idempotent re-merge.

    execute_auto_merge deliberately returns the existing merge row when the
    merged side is already retired WITH a matching audit row — that is how a
    concurrent duplicate call stays safe. Only a retired side WITHOUT such a
    row is an error.
    """
    new_id = await _mk_lesson(db_pool, "merged away")
    canonical_id = await _mk_lesson(db_pool, "canonical survivor")
    verdict = JudgeVerdict("duplicate", None, 0.95, "test")

    try:
        first = await execute_auto_merge(
            db_pool, new_lesson_id=new_id, canonical_id=canonical_id,
            verdict=verdict, cosine=0.95, judge_model="test",
        )
        second = await execute_auto_merge(
            db_pool, new_lesson_id=new_id, canonical_id=canonical_id,
            verdict=verdict, cosine=0.95, judge_model="test",
        )
        assert first == second, "re-merge should return the original audit row"
    finally:
        await _cleanup(db_pool, [new_id, canonical_id])
