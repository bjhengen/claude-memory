"""Backlog analysis helpers: pair generation, judge-and-record, report rendering."""

import logging
from typing import Any

import asyncpg
from anthropic import AsyncAnthropic

from src.consolidation.judge import adjudicate

logger = logging.getLogger(__name__)


async def generate_pairs(
    pool: asyncpg.Pool,
    cosine_threshold: float,
) -> list[dict[str, Any]]:
    """
    Return every unique live-lesson pair with pairwise cosine >= threshold.

    Canonical ordering: lesson_a_id < lesson_b_id. One row per unordered pair.
    Both lessons must have embeddings and be non-retired.

    Returns rows sorted by cosine descending so --limit runs judge the
    highest-signal pairs first.
    """
    rows = await pool.fetch(
        """
        SELECT a.id AS lesson_a_id, b.id AS lesson_b_id,
               (1 - (a.embedding <=> b.embedding)) AS cosine,
               a.title AS a_title, a.content AS a_content,
               b.title AS b_title, b.content AS b_content
        FROM lessons a
        JOIN lessons b ON a.id < b.id
        WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND a.retired_at IS NULL AND b.retired_at IS NULL
          AND (1 - (a.embedding <=> b.embedding)) >= $1
        ORDER BY cosine DESC
        """,
        cosine_threshold,
    )
    return [dict(r) for r in rows]


async def judge_and_record(
    pool: asyncpg.Pool,
    anthropic: AsyncAnthropic,
    pair: dict[str, Any],
    batch_run_id: str,
    judge_model: str,
    timeout: float,
) -> None:
    """
    Call the v5 judge for one pair and write the verdict to backlog_analysis.

    Idempotent: if the (batch_run_id, a, b) row already exists, the INSERT
    is a no-op via ON CONFLICT DO NOTHING. Errors in the judge call are
    recorded as verdict=unrelated, confidence=0.0 (same fallback as v5).
    """
    verdict = await adjudicate(
        anthropic,
        new_title=pair["a_title"], new_content=pair["a_content"],
        candidate_title=pair["b_title"], candidate_content=pair["b_content"],
        model=judge_model, timeout=timeout,
    )
    try:
        await pool.execute(
            """
            INSERT INTO backlog_analysis
              (batch_run_id, lesson_a_id, lesson_b_id, cosine_similarity,
               judge_model, verdict, direction, confidence, reasoning)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (batch_run_id, lesson_a_id, lesson_b_id) DO NOTHING
            """,
            batch_run_id, pair["lesson_a_id"], pair["lesson_b_id"],
            float(pair["cosine"]), judge_model, verdict.relationship,
            verdict.direction, verdict.confidence, verdict.reasoning,
        )
    except Exception as e:
        logger.warning(
            "backlog insert failed for pair (%s,%s): %s",
            pair["lesson_a_id"], pair["lesson_b_id"], e,
        )
