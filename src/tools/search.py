"""Search tools: search, search_lessons."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding


@mcp.tool()
async def search(query: str, limit: int = 5, ctx: Context = None) -> str:
    """
    Semantic search across lessons, patterns, and session history.
    Returns the most relevant matches based on meaning, not just keywords.

    Args:
        query: What you're looking for (natural language)
        limit: Maximum number of results (default 5)
    """
    app = ctx.request_context.lifespan_context

    # Generate embedding for query
    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    # Search using the semantic_search function
    rows = await app.db.fetch(
        "SELECT * FROM semantic_search($1::vector, $2)",
        embedding_str, limit
    )

    if not rows:
        return json.dumps({"results": [], "message": "No matches found"})

    results = []
    for row in rows:
        results.append({
            "type": row["source_type"],
            "id": row["source_id"],
            "title": row["title"],
            "content": row["content"][:500] if row["content"] else None,
            "similarity": round(row["similarity"], 3)
        })

    return json.dumps({"results": results})


@mcp.tool()
async def search_lessons(
    query: str = None,
    project: str = None,
    tags: list[str] = None,
    severity: str = None,
    limit: int = 10,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    Search lessons with optional filters.

    Args:
        query: Semantic search query (optional)
        project: Filter by project name (optional)
        tags: Filter by tags (optional)
        severity: Filter by severity: critical, important, tip (optional)
        limit: Maximum results
        include_retired: Include retired lessons (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []

    if query:
        # When query is provided, embedding is $1, so filter params start at $2
        embedding = await get_embedding(app.openai, query)
        embedding_str = format_embedding(embedding)
        params.append(embedding_str)
        param_idx = 2
    else:
        param_idx = 1

    if project:
        conditions.append(f"p.name = ${param_idx}")
        params.append(project)
        param_idx += 1

    if severity:
        conditions.append(f"l.severity = ${param_idx}")
        params.append(severity)
        param_idx += 1

    if tags:
        conditions.append(f"l.tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    if not include_retired:
        conditions.append("l.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    if query:
        sql = f"""
            SELECT l.*, p.name as project_name,
                   1 - (l.embedding <=> $1::vector) as similarity
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {where_clause} AND l.embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT ${param_idx}
        """
        params.append(limit)
    else:
        sql = f"""
            SELECT l.*, p.name as project_name
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {where_clause}
            ORDER BY l.learned_at DESC
            LIMIT ${param_idx}
        """
        params.append(limit)

    rows = await app.db.fetch(sql, *params)

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "project": row.get("project_name"),
            "tags": row["tags"],
            "severity": row["severity"],
            "learned_at": row["learned_at"].isoformat() if row["learned_at"] else None,
            "similarity": round(row["similarity"], 3) if "similarity" in row.keys() else None
        })

    return json.dumps({"lessons": results})
