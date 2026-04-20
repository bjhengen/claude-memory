"""Candidate finder: top-k nearest non-retired lessons above a cosine threshold."""

from typing import Any

import asyncpg


async def find_candidates(
    pool: asyncpg.Pool,
    query_embedding: list[float],
    new_lesson_id: int,
    project_id: int | None,
    cosine_threshold: float,
    top_k: int,
) -> list[dict[str, Any]]:
    """
    Return up to `top_k` lessons with cosine similarity >= threshold.

    - Excludes the new lesson itself (by id)
    - Excludes retired lessons
    - Scopes to same project_id OR NULL project on either side
    """
    emb_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    rows = await pool.fetch(
        """
        SELECT id, title, content, project_id, tags, severity,
               upvotes, downvotes,
               (1 - (embedding <=> $1::vector)) AS cosine
        FROM lessons
        WHERE embedding IS NOT NULL
          AND retired_at IS NULL
          AND id <> $2
          AND ($3::int IS NULL OR project_id = $3 OR project_id IS NULL)
          AND (1 - (embedding <=> $1::vector)) >= $4
        ORDER BY embedding <=> $1::vector
        LIMIT $5
        """,
        emb_str, new_lesson_id, project_id, cosine_threshold, top_k
    )

    return [dict(r) for r in rows]
