"""Search tools: search, search_lessons, find_context."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


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

    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    rows = await app.db.fetch(
        "SELECT * FROM semantic_search($1::vector, $2, $3)",
        embedding_str, query, limit
    )

    if not rows:
        return json.dumps({"results": [], "message": "No matches found"})

    results = []
    for row in rows:
        result = {
            "type": row["source_type"],
            "id": row["source_id"],
            "title": row["title"],
            "content": row["content"][:500] if row["content"] else None,
            "similarity": round(row["similarity"], 3),
            "effective_score": round(row["effective_score"], 3),
        }
        if row["source_type"] == "lesson":
            result["upvotes"] = row["upvotes"]
            result["downvotes"] = row["downvotes"]
        results.append(result)

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
        # When query is provided: $1 = embedding, $2 = query text
        # Filter params start at $3
        embedding = await get_embedding(app.openai, query)
        embedding_str = format_embedding(embedding)
        params.append(embedding_str)
        params.append(query)
        param_idx = 3
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
                   1 - (l.embedding <=> $1::vector) as similarity,
                   CASE WHEN l.tsv @@ plainto_tsquery('english', $2)
                        THEN ts_rank(l.tsv, plainto_tsquery('english', $2)) * 0.3
                        ELSE 0.0
                   END as keyword_boost,
                   CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                        THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                        ELSE 1.0
                   END as confidence,
                   (SELECT COUNT(*) FROM annotations a WHERE a.entity_type = 'lesson' AND a.entity_id = l.id) as annotation_count
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {where_clause} AND l.embedding IS NOT NULL
            ORDER BY (1 - (l.embedding <=> $1::vector)
                      + CASE WHEN l.tsv @@ plainto_tsquery('english', $2)
                             THEN ts_rank(l.tsv, plainto_tsquery('english', $2)) * 0.3
                             ELSE 0.0 END)
                     * CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                            THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                            ELSE 1.0 END
                     DESC
            LIMIT ${param_idx}
        """
        params.append(limit)
    else:
        sql = f"""
            SELECT l.*, p.name as project_name,
                   (SELECT COUNT(*) FROM annotations a WHERE a.entity_type = 'lesson' AND a.entity_id = l.id) as annotation_count
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
        result = {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "project": row.get("project_name"),
            "tags": row["tags"],
            "severity": row["severity"],
            "learned_at": row["learned_at"].isoformat() if row["learned_at"] else None,
            "similarity": round(row["similarity"], 3) if "similarity" in row.keys() else None,
            "upvotes": row.get("upvotes", 0),
            "downvotes": row.get("downvotes", 0),
            "annotation_count": row.get("annotation_count", 0),
        }
        results.append(result)

    return json.dumps({"lessons": results})


@mcp.tool()
async def find_context(
    query: str,
    project: str = None,
    tiers: list[str] = None,
    limit_per_tier: int = 3,
    ctx: Context = None
) -> str:
    """
    Unified tiered retrieval. Searches across agents, specifications, lessons,
    and MCP tools in one call. Use this to orient yourself on a task.

    Args:
        query: Natural language task description
        project: Scope to a project
        tiers: Which tiers to search: ['agents', 'specs', 'lessons', 'mcp_tools']. Defaults to all.
        limit_per_tier: Max results per tier (default 3)
    """
    app = ctx.request_context.lifespan_context

    # Generate a single embedding for all tier searches
    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    # Resolve project if provided
    project_id = None
    if project:
        project_id = await resolve_project_id(app.db, project)

    active_tiers = tiers or ["agents", "specs", "lessons", "mcp_tools"]
    result = {"query": query}

    # --- Agents tier ---
    if "agents" in active_tiers:
        agent_conditions = ["a.embedding IS NOT NULL", "a.retired_at IS NULL"]
        agent_params = [embedding_str]
        agent_idx = 2

        if project_id:
            agent_conditions.append(f"(a.project_id = ${agent_idx} OR a.project_id IS NULL)")
            agent_params.append(project_id)
            agent_idx += 1

        agent_where = " AND ".join(agent_conditions)
        agent_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT a.name, a.description, a.model, a.triggers,
                   p.name as project_name,
                   1 - (a.embedding <=> $1::vector) as similarity
            FROM agent_specs a
            LEFT JOIN projects p ON a.project_id = p.id
            WHERE {agent_where}
            ORDER BY similarity DESC
            LIMIT ${agent_idx}
            """,
            *agent_params
        )

        # Add trigger keyword scoring
        task_lower = query.lower()
        agents = []
        for row in rows:
            matched = [t for t in (row["triggers"] or []) if t.lower() in task_lower]
            semantic = float(row["similarity"])
            trigger_bonus = min(len(matched), 3) / 3.0 * 0.4
            combined = semantic * 0.6 + trigger_bonus
            confidence = "high" if combined >= 0.7 else "medium" if combined >= 0.5 else "low"
            agents.append({
                "name": row["name"],
                "description": row["description"],
                "model": row["model"],
                "project": row["project_name"],
                "confidence": confidence,
                "similarity": round(combined, 3),
                "matched_triggers": matched
            })
        agents.sort(key=lambda x: x["similarity"], reverse=True)
        result["agents"] = agents

    # --- Specifications tier ---
    if "specs" in active_tiers:
        spec_conditions = ["s.embedding IS NOT NULL", "s.retired_at IS NULL"]
        spec_params = [embedding_str]
        spec_idx = 2

        if project_id:
            spec_conditions.append(f"s.project_id = ${spec_idx}")
            spec_params.append(project_id)
            spec_idx += 1

        spec_where = " AND ".join(spec_conditions)
        spec_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT s.id, s.title, s.subsystem, s.summary, s.format_hints,
                   p.name as project_name,
                   1 - (s.embedding <=> $1::vector) as similarity
            FROM specifications s
            LEFT JOIN projects p ON s.project_id = p.id
            WHERE {spec_where}
            ORDER BY similarity DESC
            LIMIT ${spec_idx}
            """,
            *spec_params
        )

        result["specifications"] = [
            {
                "id": row["id"],
                "title": row["title"],
                "subsystem": row["subsystem"],
                "summary": row["summary"],
                "format_hints": row["format_hints"],
                "project": row["project_name"],
                "similarity": round(row["similarity"], 3)
            }
            for row in rows
        ]

    # --- Lessons tier ---
    if "lessons" in active_tiers:
        lesson_conditions = ["l.embedding IS NOT NULL", "l.retired_at IS NULL"]
        lesson_params = [embedding_str, query]
        lesson_idx = 3

        if project_id:
            lesson_conditions.append(f"l.project_id = ${lesson_idx}")
            lesson_params.append(project_id)
            lesson_idx += 1

        lesson_where = " AND ".join(lesson_conditions)
        lesson_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT l.id, l.title, l.content, l.tags, l.severity,
                   l.upvotes, l.downvotes,
                   p.name as project_name,
                   1 - (l.embedding <=> $1::vector) as similarity,
                   CASE WHEN l.tsv @@ plainto_tsquery('english', $2)
                        THEN ts_rank(l.tsv, plainto_tsquery('english', $2)) * 0.3
                        ELSE 0.0
                   END as keyword_boost,
                   CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                        THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                        ELSE 1.0
                   END as confidence,
                   (SELECT COUNT(*) FROM annotations a WHERE a.entity_type = 'lesson' AND a.entity_id = l.id) as annotation_count
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {lesson_where}
            ORDER BY (1 - (l.embedding <=> $1::vector)
                      + CASE WHEN l.tsv @@ plainto_tsquery('english', $2)
                             THEN ts_rank(l.tsv, plainto_tsquery('english', $2)) * 0.3
                             ELSE 0.0 END)
                     * CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                            THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                            ELSE 1.0 END
                     DESC
            LIMIT ${lesson_idx}
            """,
            *lesson_params
        )

        result["lessons"] = [
            {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"][:300] if row["content"] else None,
                "tags": row["tags"],
                "severity": row["severity"],
                "project": row["project_name"],
                "similarity": round(row["similarity"], 3),
                "upvotes": row["upvotes"],
                "downvotes": row["downvotes"],
                "annotation_count": row["annotation_count"],
            }
            for row in rows
        ]

    # --- MCP tools tier ---
    if "mcp_tools" in active_tiers:
        mcp_conditions = ["t.embedding IS NOT NULL", "s.retired_at IS NULL"]
        mcp_params = [embedding_str]
        mcp_idx = 2
        mcp_joins = ""

        if project_id:
            mcp_joins = "JOIN mcp_server_projects sp ON s.id = sp.server_id"
            mcp_conditions.append(f"sp.project_id = ${mcp_idx}")
            mcp_params.append(project_id)
            mcp_idx += 1

        mcp_where = " AND ".join(mcp_conditions)
        mcp_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT t.tool_name, t.description as tool_description,
                   s.name as server_name, s.url, s.transport,
                   m.name as machine_name,
                   1 - (t.embedding <=> $1::vector) as similarity
            FROM mcp_server_tools t
            JOIN mcp_servers s ON t.server_id = s.id
            LEFT JOIN machines m ON s.machine_id = m.id
            {mcp_joins}
            WHERE {mcp_where}
            ORDER BY similarity DESC
            LIMIT ${mcp_idx}
            """,
            *mcp_params
        )

        result["mcp_tools"] = [
            {
                "tool": row["tool_name"],
                "description": row["tool_description"],
                "server": row["server_name"],
                "url": row["url"],
                "transport": row["transport"],
                "machine": row["machine_name"],
                "similarity": round(row["similarity"], 3)
            }
            for row in rows
        ]

    return json.dumps(result)
