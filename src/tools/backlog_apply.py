"""v5.2 backlog batch-apply tool.

Converts high-confidence rows from backlog_analysis into real lesson_merges
entries via the existing v5 execute_auto_merge / execute_auto_supersede helpers.
"""

from datetime import datetime
from typing import Any

import asyncpg


async def _pick_canonical(conn, a_id: int, b_id: int) -> tuple[int, int]:
    """
    Choose which lesson survives a duplicate merge when neither side is "new."

    Rule: higher upvotes wins → older learned_at wins → lower id wins.
    Returns (canonical_id, merged_id). canonical is the survivor.
    """
    rows = await conn.fetch(
        "SELECT id, COALESCE(upvotes, 0) AS upvotes, learned_at "
        "FROM lessons WHERE id = ANY($1)",
        [a_id, b_id],
    )
    if len(rows) != 2:
        raise ValueError(f"expected 2 lessons for ids ({a_id}, {b_id}), got {len(rows)}")

    # Sort ascending by sort_key; first element wins.
    # - upvotes: higher wins → negate
    # - learned_at: older wins → pass through; NULL coalesces to datetime.max
    #   (since learned_at is nullable, Python < would raise TypeError without coalesce)
    # - id: lower wins → pass through
    def sort_key(r):
        # NULL learned_at is "unknown age"; fall through to the id tiebreak rather
        # than letting NULL rows win the age comparison via datetime.min coalesce.
        ts = r["learned_at"] or datetime.max
        return (-r["upvotes"], ts, r["id"])

    sorted_rows = sorted(rows, key=sort_key)
    canonical_id = sorted_rows[0]["id"]
    merged_id = sorted_rows[1]["id"]
    return canonical_id, merged_id


def classify_eligibility(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Partition rows into (eligible, skip).

    Skip reasons (checked in order; first match wins):
      - already_retired — either lesson has retired_at set
      - already_merged  — either lesson appears in lesson_merges (not reversed)
    """
    eligible = []
    skip = []
    for r in rows:
        if r["a_retired"] or r["b_retired"]:
            skip.append({**r, "reason": "already_retired"})
        elif r["a_in_merges"] or r["b_in_merges"]:
            skip.append({**r, "reason": "already_merged"})
        else:
            eligible.append(r)
    return eligible, skip


async def fetch_candidate_rows(
    pool: asyncpg.Pool,
    batch_run_id: str,
    verdict_in: list[str],
    confidence_gte: float,
) -> list[dict[str, Any]]:
    """
    Return backlog_analysis rows matching filters, each annotated with:
      - a_retired, b_retired (bool): current retirement status
      - a_in_merges, b_in_merges (bool): participation in non-reversed merges
      - a_title, b_title: for report/preview display
    Sorted by confidence DESC.
    """
    rows = await pool.fetch(
        """
        WITH merged_lesson_ids AS (
          SELECT canonical_id AS lesson_id FROM lesson_merges WHERE reversed_at IS NULL
          UNION
          SELECT merged_id AS lesson_id FROM lesson_merges WHERE reversed_at IS NULL
        )
        SELECT
          ba.lesson_a_id, ba.lesson_b_id, ba.verdict, ba.direction,
          ba.confidence, ba.cosine_similarity, ba.reasoning, ba.judge_model,
          la.title AS a_title, (la.retired_at IS NOT NULL) AS a_retired,
          lb.title AS b_title, (lb.retired_at IS NOT NULL) AS b_retired,
          (la.id IN (SELECT lesson_id FROM merged_lesson_ids)) AS a_in_merges,
          (lb.id IN (SELECT lesson_id FROM merged_lesson_ids)) AS b_in_merges
        FROM backlog_analysis ba
        JOIN lessons la ON la.id = ba.lesson_a_id
        JOIN lessons lb ON lb.id = ba.lesson_b_id
        WHERE ba.batch_run_id = $1
          AND ba.verdict = ANY($2)
          AND ba.confidence >= $3
        ORDER BY ba.confidence DESC
        """,
        batch_run_id, verdict_in, confidence_gte,
    )
    return [dict(r) for r in rows]
