"""Tests for the get_consolidation_stats helper (pure SQL, no MCP context)."""

import pytest

from src.tools.consolidation import _compute_stats


async def _insert_lesson(conn, title, learned_at=None):
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    if learned_at is None:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding) "
            "VALUES ($1, $2, $3::vector) RETURNING id",
            title, "x", emb,
        )
    else:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, learned_at) "
            "VALUES ($1, $2, $3::vector, $4) RETURNING id",
            title, "x", emb, learned_at,
        )
    return row["id"]


async def _insert_merge(conn, canonical_id, merged_id, action, created_at,
                        auto_decided=True, confidence=0.95, reversed_at=None,
                        decided_by=None):
    row = await conn.fetchrow(
        """
        INSERT INTO lesson_merges
          (canonical_id, merged_id, action, judge_model, judge_confidence,
           judge_reasoning, cosine_similarity, auto_decided, decided_by,
           transferred_upvotes, transferred_downvotes, created_at, reversed_at,
           reversed_by, reversed_reason)
        VALUES ($1, $2, $3, 'claude-haiku-4-5-20251001', $4, 'r', 0.9,
                $5, $6, 0, 0, $7, $8, $9, $10)
        RETURNING id
        """,
        canonical_id, merged_id, action, confidence, auto_decided, decided_by,
        created_at, reversed_at,
        'tester' if reversed_at else None,
        'test reversal' if reversed_at else None,
    )
    return row["id"]


@pytest.mark.asyncio
async def test_stats_empty_db_returns_zeros(db_pool):
    # Stats are global aggregations — full cleanup, not prefix-filtered.
    # Safe on the dedicated test DB; would not be safe on prod.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM consolidation_queue")
        await conn.execute("DELETE FROM lesson_merges")
        await conn.execute("DELETE FROM lesson_conflicts")
        await conn.execute("DELETE FROM annotations WHERE entity_type='lesson'")
        await conn.execute("DELETE FROM lessons")

    stats = await _compute_stats(db_pool, days=7)

    assert stats["window_days"] == 7
    assert stats["new_lessons"] == 0
    assert stats["merges_in_window"] == 0
    assert stats["duplicates_merged"] == 0
    assert stats["supersedes"] == 0
    assert stats["auto_decided_count"] == 0
    assert stats["human_decided_count"] == 0
    assert stats["avg_confidence"] is None
    assert stats["reversals_in_window"] == 0
    assert stats["conflicts_flagged"] == 0
    assert stats["conflicts_resolved"] == 0
    assert stats["conflicts_pending"] == 0
    assert stats["queue_depth"] == 0


@pytest.mark.asyncio
async def test_stats_window_filters_and_breakdown(db_pool):
    from datetime import datetime, timezone, timedelta

    # Stats are global aggregations — full cleanup, not prefix-filtered.
    # Safe on the dedicated test DB; would not be safe on prod.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM consolidation_queue")
        await conn.execute("DELETE FROM lesson_merges")
        await conn.execute("DELETE FROM lesson_conflicts")
        await conn.execute("DELETE FROM annotations WHERE entity_type='lesson'")
        await conn.execute("DELETE FROM lessons")

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        recent = now_naive - timedelta(days=3)
        old = now_naive - timedelta(days=30)

        # Lessons: 1 recent + 1 old → only recent counts
        a = await _insert_lesson(conn, "T_ST_recent_a", learned_at=recent)
        b = await _insert_lesson(conn, "T_ST_recent_b", learned_at=recent)
        c = await _insert_lesson(conn, "T_ST_old_c", learned_at=old)
        d = await _insert_lesson(conn, "T_ST_recent_d", learned_at=recent)
        e = await _insert_lesson(conn, "T_ST_recent_e", learned_at=recent)
        f = await _insert_lesson(conn, "T_ST_recent_f", learned_at=recent)

        # Merges: 1 recent auto-merged (dup) + 1 recent human-superseded + 1 old (out of window) + 1 reversed
        await _insert_merge(conn, a, b, "merged", recent, auto_decided=True, confidence=0.95)
        await _insert_merge(conn, d, e, "superseded", recent, auto_decided=False,
                            decided_by="bjhengen", confidence=0.97)
        await _insert_merge(conn, a, c, "merged", old, auto_decided=True, confidence=0.90)
        await _insert_merge(conn, a, f, "merged", old, auto_decided=True, confidence=0.92,
                            reversed_at=recent)

        # Conflicts: 1 flagged in window, 1 resolved in window, 1 pending (flagged old)
        # lesson_conflicts has CHECK (lesson_a_id < lesson_b_id), so use min/max.
        ab_lo, ab_hi = min(a, b), max(a, b)
        de_lo, de_hi = min(d, e), max(d, e)
        cf_lo, cf_hi = min(c, f), max(c, f)
        await conn.execute(
            "INSERT INTO lesson_conflicts (lesson_a_id, lesson_b_id, judge_model, "
            "judge_confidence, judge_reasoning, cosine_similarity, flagged_at) "
            "VALUES ($1, $2, 'm', 0.7, 'r', 0.9, $3)",
            ab_lo, ab_hi, recent,
        )
        await conn.execute(
            "INSERT INTO lesson_conflicts (lesson_a_id, lesson_b_id, judge_model, "
            "judge_confidence, judge_reasoning, cosine_similarity, flagged_at, "
            "resolved_at, resolved_by, resolution) "
            "VALUES ($1, $2, 'm', 0.8, 'r', 0.9, $3, $4, 'tester', 'kept_both')",
            de_lo, de_hi, old, recent,
        )
        await conn.execute(
            "INSERT INTO lesson_conflicts (lesson_a_id, lesson_b_id, judge_model, "
            "judge_confidence, judge_reasoning, cosine_similarity, flagged_at) "
            "VALUES ($1, $2, 'm', 0.75, 'r', 0.9, $3)",
            cf_lo, cf_hi, old,
        )

        # Queue: 1 pending
        await conn.execute(
            "INSERT INTO consolidation_queue (new_lesson_id, candidate_lesson_id, "
            "proposed_action, judge_model, judge_confidence, judge_reasoning, "
            "cosine_similarity) VALUES ($1, $2, 'merged', 'm', 0.8, 'r', 0.9)",
            a, e,
        )

    stats = await _compute_stats(db_pool, days=7)

    assert stats["window_days"] == 7
    assert stats["new_lessons"] == 5  # recent a, b, d, e, f — not c (old)
    assert stats["merges_in_window"] == 2  # 2 non-reversed in window; old + reversed excluded
    assert stats["duplicates_merged"] == 1
    assert stats["supersedes"] == 1
    assert stats["auto_decided_count"] == 1
    assert stats["human_decided_count"] == 1
    assert stats["avg_confidence"] is not None
    # Avg of 0.95 and 0.97 = 0.96
    assert abs(stats["avg_confidence"] - 0.96) < 0.01
    assert stats["reversals_in_window"] == 1
    assert stats["conflicts_flagged"] == 1  # 1 recent
    assert stats["conflicts_resolved"] == 1  # 1 resolved in window
    # pending = all unresolved regardless of window: 1 recent-flagged unresolved + 1 old-flagged unresolved
    assert stats["conflicts_pending"] == 2
    assert stats["queue_depth"] == 1
