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
