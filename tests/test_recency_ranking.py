"""Recency-aware, confidence-free ranking (migration v9).

Review 2026-07-06 (P2.1): no ranking surface had a recency signal — a
January lesson ranked identically to yesterday's — and the v4 vote-confidence
multiplier was inert (unrated lessons scored the 1.0 cap; zero rated lessons
in 14 months). P2.2: the search tool's source_agent post-filter oversampled
limit*5 then dropped rows — the filter now lives inside semantic_search().
"""

import pytest

# Distinct sparse direction — residue [0.1]*1536 rows score cosine ≈ 0.026.
_EMB = [0.0, 1.0] + [0.0] * 1534
_EMB_STR = "[" + ",".join(str(x) for x in _EMB) + "]"


async def _insert_lesson(conn, title, age_days, content="same advice text",
                         downvotes=0, source_agent="claude"):
    row = await conn.fetchrow(
        """INSERT INTO lessons (title, content, embedding, downvotes, source_agent, learned_at)
           VALUES ($1, $2, $3::vector, $4, $5, NOW() - ($6 || ' days')::interval)
           RETURNING id""",
        title, content, _EMB_STR, downvotes, source_agent, str(age_days),
    )
    return row["id"]


@pytest.mark.asyncio
async def test_fresh_lesson_outranks_stale_twin(db_pool):
    """Same content, same embedding — the recent lesson must rank first."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_RR_%'")
        old_id = await _insert_lesson(conn, "T_RR_old", age_days=500)
        new_id = await _insert_lesson(conn, "T_RR_new", age_days=0)
    try:
        rows = await db_pool.fetch(
            "SELECT * FROM semantic_search($1::vector, $2, 5)",
            _EMB_STR, "same advice text")
        ids = [r["source_id"] for r in rows if r["source_type"] == "lesson"
               and r["source_id"] in (old_id, new_id)]
        assert ids.index(new_id) < ids.index(old_id)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = ANY($1)", [old_id, new_id])


@pytest.mark.asyncio
async def test_recency_penalty_is_bounded(db_pool):
    """The floor (0.7) keeps a strongly-matching old lesson above a weak
    fresh one — recency tunes, it doesn't dominate."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_RRB_%'")
        # Old lesson: exact embedding match. Fresh lesson: weaker match.
        strong_old = await _insert_lesson(conn, "T_RRB_strong_old", age_days=700)
        weak_fresh_row = await conn.fetchrow(
            """INSERT INTO lessons (title, content, embedding, source_agent, learned_at)
               VALUES ('T_RRB_weak_fresh', 'different topic entirely', $1::vector, 'claude', NOW())
               RETURNING id""",
            "[" + ",".join(str(x) for x in ([0.87, 0.5] + [0.0] * 1534)) + "]",
        )
        weak_fresh = weak_fresh_row["id"]
    try:
        rows = await db_pool.fetch(
            "SELECT * FROM semantic_search($1::vector, $2, 5)",
            _EMB_STR, "zzqx nomatch")
        scores = {r["source_id"]: r["effective_score"] for r in rows
                  if r["source_type"] == "lesson"}
        assert scores[strong_old] > scores[weak_fresh]
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id = ANY($1)", [strong_old, weak_fresh])


@pytest.mark.asyncio
async def test_votes_no_longer_scale_score(db_pool):
    """Confidence multiplier removed: a downvoted lesson scores the same as
    its unrated twin (votes surface in payloads, not in ranking)."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_RRV_%'")
        rated = await _insert_lesson(conn, "T_RRV_rated", age_days=10, downvotes=3)
        unrated = await _insert_lesson(conn, "T_RRV_unrated", age_days=10)
    try:
        rows = await db_pool.fetch(
            "SELECT * FROM semantic_search($1::vector, $2, 5)",
            _EMB_STR, "same advice text")
        scores = {r["source_id"]: r["effective_score"] for r in rows
                  if r["source_type"] == "lesson"}
        assert scores[rated] == pytest.approx(scores[unrated], rel=1e-6)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = ANY($1)", [rated, unrated])


@pytest.mark.asyncio
async def test_source_agent_filter_in_sql(db_pool):
    """filter_source_agent restricts results inside the function — full
    recall for the requested agent, no post-filtering."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_RRA_%'")
        claude_id = await _insert_lesson(conn, "T_RRA_claude", 0, source_agent="claude")
        codex_id = await _insert_lesson(conn, "T_RRA_codex", 0, source_agent="codex")
    try:
        rows = await db_pool.fetch(
            "SELECT * FROM semantic_search($1::vector, $2, 10, 'codex')",
            _EMB_STR, "same advice text")
        ids = [r["source_id"] for r in rows if r["source_type"] == "lesson"]
        assert codex_id in ids
        assert claude_id not in ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id = ANY($1)", [claude_id, codex_id])
