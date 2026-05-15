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
    source_agent: str,           # REQUIRED — cross-agent skip
) -> list[dict[str, Any]]:
    """Return up to `top_k` lessons with cosine >= threshold AND same source_agent.

    Required `source_agent`: callers must explicitly specify the agent family
    of the new lesson. Filtering same-agent prevents cross-agent auto-merge.
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
          AND source_agent = $5
        ORDER BY embedding <=> $1::vector
        LIMIT $6
        """,
        emb_str, new_lesson_id, project_id, cosine_threshold, source_agent, top_k,
    )
    return [dict(r) for r in rows]
