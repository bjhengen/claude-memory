"""Tests for pick_canonical — deterministic winner selection for duplicate merges.

Policy (review 2026-07-06, P1.2): higher upvotes → longer content → newer
learned_at (NULL sorts oldest) → higher id. Comprehensiveness outranks age:
the old "older wins" tiebreak retired the newer, more detailed write-up
(lesson #1327).
"""

import pytest

from src.tools.backlog_apply import _pick_canonical


async def _insert_lesson(conn, title, content="x", upvotes=0, downvotes=0,
                         learned_at=None):
    emb_str = "[" + ",".join(["0.1"] * 1536) + "]"
    if learned_at is None:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, downvotes) "
            "VALUES ($1, $2, $3::vector, $4, $5) RETURNING id",
            title, content, emb_str, upvotes, downvotes,
        )
    else:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, downvotes, learned_at) "
            "VALUES ($1, $2, $3::vector, $4, $5, $6) RETURNING id",
            title, content, emb_str, upvotes, downvotes, learned_at,
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
async def test_pick_canonical_upvotes_outrank_length(db_pool):
    """A rated lesson keeps its identity even against a longer duplicate."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_UPLEN\\_%' ESCAPE '\\'")
        short_rated = await _insert_lesson(conn, "T_PC_UPLEN_A", content="short", upvotes=2)
        long_unrated = await _insert_lesson(conn, "T_PC_UPLEN_B", content="long " * 100)

        canonical, merged = await _pick_canonical(conn, short_rated, long_unrated)
        assert canonical == short_rated
        assert merged == long_unrated


@pytest.mark.asyncio
async def test_pick_canonical_longer_content_wins_on_upvote_tie(db_pool):
    from datetime import datetime, timezone, timedelta
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_LEN\\_%' ESCAPE '\\'")
        earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        later = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        # The OLDER lesson is the comprehensive one here — length must win
        # regardless of which side is newer.
        long_old = await _insert_lesson(
            conn, "T_PC_LEN_LONG", content="rich detail " * 50, learned_at=earlier)
        short_new = await _insert_lesson(
            conn, "T_PC_LEN_SHORT", content="thin", learned_at=later)

        canonical, merged = await _pick_canonical(conn, long_old, short_new)
        assert canonical == long_old
        assert merged == short_new


@pytest.mark.asyncio
async def test_pick_canonical_newer_wins_on_length_tie(db_pool):
    from datetime import datetime, timezone, timedelta
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_TIE\\_%' ESCAPE '\\'")
        earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        later = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        a = await _insert_lesson(conn, "T_PC_TIE_EARLY", upvotes=2, learned_at=earlier)
        b = await _insert_lesson(conn, "T_PC_TIE_LATE", upvotes=2, learned_at=later)

        canonical, merged = await _pick_canonical(conn, a, b)
        assert canonical == b  # newer wins when votes and length tie
        assert merged == a


@pytest.mark.asyncio
async def test_pick_canonical_higher_id_wins_on_full_tie(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_SAME\\_%' ESCAPE '\\'")
        a = await _insert_lesson(conn, "T_PC_SAME_A", upvotes=0)
        b = await _insert_lesson(conn, "T_PC_SAME_B", upvotes=0)

        canonical, merged = await _pick_canonical(conn, a, b)
        # Full tie → higher id (the more recent insert) wins
        assert canonical > merged
        assert {canonical, merged} == {a, b}


@pytest.mark.asyncio
async def test_pick_canonical_handles_null_learned_at(db_pool):
    """learned_at is nullable; NULL must not crash the sort and sorts oldest."""
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
        # NULL learned_at sorts oldest; row_b has a real NOW() timestamp → wins.
        assert canonical == row_b["id"]
        assert merged == row_a["id"]
