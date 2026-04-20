"""Backlog analysis helpers: pair generation, judge-and-record, report rendering."""

from typing import Any

import asyncpg


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
