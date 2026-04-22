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


import json
import logging

from mcp.server.fastmcp import Context

from src.server import mcp
from src.consolidation.actor import execute_auto_merge, execute_auto_supersede
from src.consolidation.judge import JudgeVerdict

_logger = logging.getLogger(__name__)


@mcp.tool()
async def apply_backlog_batch(
    batch_run_id: str,
    confirm: bool = False,
    verdict_in: list[str] = None,
    confidence_gte: float = 0.90,
    max_apply: int = 50,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Apply high-confidence backlog_analysis rows as real lesson_merges.

    Preview-by-default: without confirm=true, returns a summary of what WOULD
    happen and writes nothing.

    Apply mode (confirm=true) requires a non-empty reviewer. Applies at most
    max_apply pairs in one call; re-call to process more.

    Args:
        batch_run_id: The batch_run_id from backlog_analysis (e.g., 'pilot-2026-04-21')
        confirm: If False (default), preview only — no DB writes
        verdict_in: Which verdict types to apply (default: ['duplicate', 'supersedes'])
        confidence_gte: Minimum confidence threshold (default 0.90)
        max_apply: Cap on pairs applied per call (default 50)
        reviewer: Required when confirm=true; recorded as decided_by in lesson_merges
    """
    if verdict_in is None:
        verdict_in = ["duplicate", "supersedes"]

    app = ctx.request_context.lifespan_context
    pool = app.db

    # Fetch + classify
    rows = await fetch_candidate_rows(pool, batch_run_id, verdict_in, confidence_gte)
    eligible, skipped = classify_eligibility(rows)
    skip_reasons: dict[str, int] = {}
    for s in skipped:
        skip_reasons[s["reason"]] = skip_reasons.get(s["reason"], 0) + 1

    if not confirm:
        first_10 = [
            {
                "lesson_a_id": r["lesson_a_id"], "lesson_b_id": r["lesson_b_id"],
                "a_title": r["a_title"], "b_title": r["b_title"],
                "verdict": r["verdict"], "direction": r["direction"],
                "confidence": float(r["confidence"]),
                "cosine": float(r["cosine_similarity"]),
                "reasoning": r["reasoning"],
            }
            for r in eligible[:10]
        ]
        return json.dumps({
            "preview": True,
            "batch_run_id": batch_run_id,
            "filters": {
                "verdict_in": verdict_in,
                "confidence_gte": confidence_gte,
            },
            "would_apply": min(len(eligible), max_apply),
            "would_skip": len(skipped),
            "skip_reasons": skip_reasons,
            "first_10": first_10,
            "total_eligible": len(eligible),
            "next_step": "call again with confirm=true, reviewer='your-name' to apply",
        })

    # Apply mode
    if not reviewer or not reviewer.strip():
        return json.dumps({"error": "reviewer is required when confirm=true"})

    to_apply = eligible[:max_apply]
    applied_merge_ids: list[int] = []
    apply_errors = 0

    for r in to_apply:
        try:
            verdict_obj = JudgeVerdict(
                relationship=r["verdict"],
                direction=r["direction"],
                confidence=float(r["confidence"]),
                reasoning=r["reasoning"],
            )
            cosine = float(r["cosine_similarity"])
            model = r["judge_model"]

            if r["verdict"] == "duplicate":
                async with pool.acquire() as conn:
                    canonical_id, merged_id = await _pick_canonical(
                        conn, r["lesson_a_id"], r["lesson_b_id"],
                    )
                merge_id = await execute_auto_merge(
                    pool,
                    new_lesson_id=merged_id,
                    canonical_id=canonical_id,
                    verdict=verdict_obj,
                    cosine=cosine,
                    judge_model=model,
                    decided_by=reviewer,
                    auto_decided=False,
                )
            elif r["verdict"] == "supersedes":
                if r["direction"] == "new→existing":
                    # A supersedes B: retire B, A survives.
                    merge_id = await execute_auto_supersede(
                        pool,
                        new_lesson_id=r["lesson_a_id"],
                        existing_lesson_id=r["lesson_b_id"],
                        verdict=verdict_obj,
                        cosine=cosine,
                        judge_model=model,
                        decided_by=reviewer,
                        auto_decided=False,
                    )
                elif r["direction"] == "existing→new":
                    # B supersedes A: retire A, B survives.
                    # execute_auto_supersede has a hard guard rejecting
                    # direction='existing→new', so use execute_auto_merge
                    # instead: merge A into B (B is canonical).
                    merge_id = await execute_auto_merge(
                        pool,
                        new_lesson_id=r["lesson_a_id"],
                        canonical_id=r["lesson_b_id"],
                        verdict=verdict_obj,
                        cosine=cosine,
                        judge_model=model,
                        decided_by=reviewer,
                        auto_decided=False,
                    )
                else:
                    _logger.warning(
                        "supersedes row missing direction: a=%s b=%s",
                        r["lesson_a_id"], r["lesson_b_id"],
                    )
                    apply_errors += 1
                    continue
            else:
                _logger.warning("unexpected verdict: %s", r["verdict"])
                apply_errors += 1
                continue

            applied_merge_ids.append(merge_id)
        except Exception as e:
            _logger.warning(
                "apply failed for pair (%s,%s): %s",
                r["lesson_a_id"], r["lesson_b_id"], e,
            )
            apply_errors += 1

    remaining = max(0, len(eligible) - len(to_apply))
    return json.dumps({
        "applied": len(applied_merge_ids),
        "skipped": len(skipped),
        "apply_errors": apply_errors,
        "skip_reasons": skip_reasons,
        "merge_ids": applied_merge_ids,
        "reviewer": reviewer,
        "batch_run_id": batch_run_id,
        "remaining_above_threshold": remaining,
    })
