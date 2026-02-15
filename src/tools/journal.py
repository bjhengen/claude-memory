"""Journal tools: write_journal, read_journal."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding


@mcp.tool()
async def write_journal(
    content: str,
    tags: list[str] = None,
    mood: str = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Write a journal entry. This is Claude's personal space for observations,
    reflections, and notes that don't fit structured lessons or patterns.

    Args:
        content: The journal entry content
        tags: Optional tags for categorization
        mood: Optional mood indicator (reflective, curious, frustrated, satisfied, etc.)
        project: Optional associated project
    """
    app = ctx.request_context.lifespan_context

    # Get project ID if specified
    project_id = None
    if project:
        row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
        if row:
            project_id = row["id"]

    # Generate embedding for searchability
    embedding = await get_embedding(app.openai, content)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO journal (content, tags, mood, project_id, embedding)
        VALUES ($1, $2, $3, $4, $5::vector)
        RETURNING id, entry_date
        """,
        content, tags or [], mood, project_id, embedding_str
    )

    return json.dumps({
        "success": True,
        "entry_id": row["id"],
        "entry_date": row["entry_date"].isoformat(),
        "message": "Journal entry saved"
    })


@mcp.tool()
async def read_journal(
    query: str = None,
    tags: list[str] = None,
    project: str = None,
    limit: int = 10,
    ctx: Context = None
) -> str:
    """
    Read journal entries. Can search semantically or filter by tags/project.

    Args:
        query: Semantic search query (optional)
        tags: Filter by tags (optional)
        project: Filter by project (optional)
        limit: Maximum entries to return
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []

    if query:
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

    if tags:
        conditions.append(f"j.tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    if query:
        sql = f"""
            SELECT j.*, p.name as project_name,
                   1 - (j.embedding <=> $1::vector) as similarity
            FROM journal j
            LEFT JOIN projects p ON j.project_id = p.id
            WHERE {where_clause} AND j.embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT ${param_idx}
        """
        params.append(limit)
    else:
        sql = f"""
            SELECT j.*, p.name as project_name
            FROM journal j
            LEFT JOIN projects p ON j.project_id = p.id
            WHERE {where_clause}
            ORDER BY j.entry_date DESC
            LIMIT ${param_idx}
        """
        params.append(limit)

    rows = await app.db.fetch(sql, *params)

    entries = []
    for row in rows:
        entries.append({
            "id": row["id"],
            "content": row["content"],
            "tags": row["tags"],
            "mood": row["mood"],
            "project": row.get("project_name"),
            "entry_date": row["entry_date"].isoformat() if row["entry_date"] else None,
            "similarity": round(row["similarity"], 3) if "similarity" in row.keys() else None
        })

    return json.dumps({"entries": entries})
