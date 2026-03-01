"""Specification document tools for managing structured project knowledge."""

import json
from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def create_spec(
    title: str,
    content: str,
    summary: str,
    project: str,
    subsystem: str = None,
    format_hints: list[str] = None,
    triggers: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Create a new specification document. Specs are structured long-form
    project knowledge (architecture docs, system designs, decision trees).

    Args:
        title: Document title (e.g., 'Save System Architecture')
        content: Full structured markdown content
        summary: 1-2 paragraph summary for search results (embedded for semantic matching)
        project: Project name (required — specs are always project-scoped)
        subsystem: Domain tag (e.g., 'networking', 'auth', 'deployment')
        format_hints: Content structure tags (e.g., ['decision-tree', 'symptom-fix-table'])
        triggers: Keywords for routing
    """
    app = ctx.request_context.lifespan_context

    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    # Generate embedding from summary
    embedding = await get_embedding(app.openai, summary)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO specifications (title, content, summary, project_id, subsystem,
                                    format_hints, triggers, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
        RETURNING id
        """,
        title, content, summary, project_id, subsystem,
        format_hints or [], triggers or [], embedding_str
    )

    return json.dumps({
        "success": True,
        "spec_id": row["id"],
        "message": f"Specification '{title}' created"
    })


@mcp.tool()
async def get_spec(spec_id: int, ctx: Context = None) -> str:
    """
    Get full specification content by ID. This is the cold-memory retrieval call —
    use when you need the detailed architecture/system documentation.

    Args:
        spec_id: ID of the specification to retrieve
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        SELECT s.*, p.name as project_name
        FROM specifications s
        LEFT JOIN projects p ON s.project_id = p.id
        WHERE s.id = $1
        """,
        spec_id
    )

    if not row:
        return json.dumps({"error": f"Specification {spec_id} not found"})

    return json.dumps({
        "id": row["id"],
        "title": row["title"],
        "subsystem": row["subsystem"],
        "content": row["content"],
        "summary": row["summary"],
        "format_hints": row["format_hints"],
        "triggers": row["triggers"],
        "project": row["project_name"],
        "version": row["version"],
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "retired": row["retired_at"] is not None
    })


@mcp.tool()
async def update_spec(
    spec_id: int,
    title: str = None,
    content: str = None,
    summary: str = None,
    subsystem: str = None,
    format_hints: list[str] = None,
    triggers: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Update a specification document. Only provided fields are changed.
    Bumps version. Resets verified_at to now. Regenerates embedding if summary changes.

    Args:
        spec_id: ID of the spec to update
        title: New title
        content: New full content
        summary: New summary (triggers re-embedding)
        subsystem: New subsystem tag
        format_hints: New format hint tags
        triggers: New trigger keywords
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT * FROM specifications WHERE id = $1", spec_id
    )
    if not existing:
        return json.dumps({"error": f"Specification {spec_id} not found"})

    updates = ["version = version + 1", "updated_at = NOW()", "verified_at = NOW()"]
    params = []
    param_idx = 1

    if title is not None:
        updates.append(f"title = ${param_idx}")
        params.append(title)
        param_idx += 1

    if content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(content)
        param_idx += 1

    if summary is not None:
        updates.append(f"summary = ${param_idx}")
        params.append(summary)
        param_idx += 1

    if subsystem is not None:
        updates.append(f"subsystem = ${param_idx}")
        params.append(subsystem)
        param_idx += 1

    if format_hints is not None:
        updates.append(f"format_hints = ${param_idx}")
        params.append(format_hints)
        param_idx += 1

    if triggers is not None:
        updates.append(f"triggers = ${param_idx}")
        params.append(triggers)
        param_idx += 1

    if len(params) == 0:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if summary changed
    if summary is not None:
        embedding = await get_embedding(app.openai, summary)
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(spec_id)
    await app.db.execute(
        f"UPDATE specifications SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({
        "success": True,
        "message": f"Specification {spec_id} ('{existing['title']}') updated"
    })


@mcp.tool()
async def list_specs(
    project: str = None,
    subsystem: str = None,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    List specification documents at summary level (no full content).

    Args:
        project: Filter by project name
        subsystem: Filter by subsystem tag
        include_retired: Include retired specs (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []
    param_idx = 1

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            conditions.append(f"s.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if subsystem:
        conditions.append(f"s.subsystem = ${param_idx}")
        params.append(subsystem)
        param_idx += 1

    if not include_retired:
        conditions.append("s.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    rows = await app.db.fetch(
        f"""
        SELECT s.id, s.title, s.subsystem, s.summary, s.format_hints,
               s.triggers, s.version, s.verified_at, s.project_id,
               p.name as project_name, s.retired_at
        FROM specifications s
        LEFT JOIN projects p ON s.project_id = p.id
        WHERE {where_clause}
        ORDER BY s.title
        """,
        *params
    )

    specs = []
    for row in rows:
        specs.append({
            "id": row["id"],
            "title": row["title"],
            "subsystem": row["subsystem"],
            "summary": row["summary"],
            "format_hints": row["format_hints"],
            "triggers": row["triggers"],
            "version": row["version"],
            "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
            "project": row["project_name"],
            "retired": row["retired_at"] is not None
        })

    return json.dumps({"specifications": specs})


@mcp.tool()
async def retire_spec(
    spec_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire a specification (soft delete).
    Retired specs are excluded from search and list by default.

    Args:
        spec_id: ID of the spec to retire
        reason: Why the spec is being retired
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, title FROM specifications WHERE id = $1", spec_id
    )
    if not existing:
        return json.dumps({"error": f"Specification {spec_id} not found"})

    await app.db.execute(
        "UPDATE specifications SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, spec_id
    )

    return json.dumps({
        "success": True,
        "message": f"Specification {spec_id} ('{existing['title']}') retired"
    })


@mcp.tool()
async def search_specs(
    query: str,
    project: str = None,
    subsystem: str = None,
    limit: int = 5,
    ctx: Context = None
) -> str:
    """
    Semantic search across specification summaries.

    Args:
        query: Natural language search query
        project: Filter by project name
        subsystem: Filter by subsystem tag
        limit: Maximum results (default 5)
    """
    app = ctx.request_context.lifespan_context

    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    conditions = ["s.embedding IS NOT NULL", "s.retired_at IS NULL"]
    params = [embedding_str]
    param_idx = 2

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            conditions.append(f"s.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if subsystem:
        conditions.append(f"s.subsystem = ${param_idx}")
        params.append(subsystem)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    params.append(limit)
    rows = await app.db.fetch(
        f"""
        SELECT s.id, s.title, s.subsystem, s.summary, s.format_hints,
               s.triggers, s.version, s.verified_at,
               p.name as project_name,
               1 - (s.embedding <=> $1::vector) as similarity
        FROM specifications s
        LEFT JOIN projects p ON s.project_id = p.id
        WHERE {where_clause}
        ORDER BY similarity DESC
        LIMIT ${param_idx}
        """,
        *params
    )

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "title": row["title"],
            "subsystem": row["subsystem"],
            "summary": row["summary"],
            "format_hints": row["format_hints"],
            "triggers": row["triggers"],
            "project": row["project_name"],
            "version": row["version"],
            "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
            "similarity": round(row["similarity"], 3)
        })

    return json.dumps({"specifications": results})
