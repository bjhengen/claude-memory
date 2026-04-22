"""Tests for _pick_canonical — deterministic winner selection for duplicate merges."""

import pytest

from src.tools.backlog_apply import _pick_canonical


async def _insert_lesson(conn, title, upvotes=0, downvotes=0, learned_at=None):
    emb_str = "[" + ",".join(["0.1"] * 1536) + "]"
    if learned_at is None:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, downvotes) "
            "VALUES ($1, $2, $3::vector, $4, $5) RETURNING id",
            title, "x", emb_str, upvotes, downvotes,
        )
    else:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, downvotes, learned_at) "
            "VALUES ($1, $2, $3::vector, $4, $5, $6) RETURNING id",
            title, "x", emb_str, upvotes, downvotes, learned_at,
        )
    return row["id"]


@pytest.mark.asyncio
async def test_pick_canonical_higher_upvotes_wins(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_%' ESCAPE '\\'")
        a = await _insert_lesson(conn, "T_PC_A", upvotes=5)
        b = await _insert_lesson(conn, "T_PC_B", upvotes=1)

        canonical, merged = await _pick_canonical(conn, a, b)
        assert canonical == a
        assert merged == b

        # Reversed input order — still picks A
        canonical, merged = await _pick_canonical(conn, b, a)
        assert canonical == a
        assert merged == b


@pytest.mark.asyncio
async def test_pick_canonical_older_wins_on_upvote_tie(db_pool):
    from datetime import datetime, timezone, timedelta
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_TIE\\_%' ESCAPE '\\'")
        earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        later = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        a = await _insert_lesson(conn, "T_PC_TIE_EARLY", upvotes=2, learned_at=earlier)
        b = await _insert_lesson(conn, "T_PC_TIE_LATE", upvotes=2, learned_at=later)

        canonical, merged = await _pick_canonical(conn, a, b)
        assert canonical == a  # older wins
        assert merged == b


@pytest.mark.asyncio
async def test_pick_canonical_lower_id_wins_on_full_tie(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_SAME\\_%' ESCAPE '\\'")
        # Insert both in one statement so timestamps are effectively equal
        a = await _insert_lesson(conn, "T_PC_SAME_A", upvotes=0)
        b = await _insert_lesson(conn, "T_PC_SAME_B", upvotes=0)

        canonical, merged = await _pick_canonical(conn, a, b)
        # a was inserted first → lower id → wins on final tiebreak
        assert canonical < merged
        assert {canonical, merged} == {a, b}


@pytest.mark.asyncio
async def test_pick_canonical_handles_null_learned_at(db_pool):
    """learned_at is nullable; NULL must not crash the Python sort."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_NULL\\_%' ESCAPE '\\'")
        emb = "[" + ",".join(["0.1"] * 1536) + "]"
        row_a = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, learned_at) "
            "VALUES ($1, $2, $3::vector, $4, NULL) RETURNING id",
            "T_PC_NULL_A", "x", emb, 0,
        )
        row_b = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes) "
            "VALUES ($1, $2, $3::vector, $4) RETURNING id",
            "T_PC_NULL_B", "x", emb, 0,
        )
        canonical, merged = await _pick_canonical(conn, row_a["id"], row_b["id"])
        # NULL learned_at is coalesced to datetime.max, so the NULL row LOSES the
        # age tiebreak. row_b has a real NOW() timestamp, so row_b wins.
        assert canonical == row_b["id"]
        assert merged == row_a["id"]
