"""Agent specification tools for managing reusable domain-expert agent definitions."""

import json
from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def register_agent(
    name: str,
    description: str,
    spec_content: str,
    summary: str = None,
    model: str = "sonnet",
    triggers: list[str] = None,
    tools: list[str] = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Register a new agent specification.

    Args:
        name: Unique agent identifier (e.g., 'network-protocol-designer')
        description: One-line purpose of this agent
        spec_content: Full markdown specification (domain knowledge, rules, checklists)
        summary: 1-2 paragraph summary for search results (embedded for semantic matching)
        model: Preferred Claude model: opus, sonnet, haiku
        triggers: Keywords for auto-routing (e.g., ['network', 'sync', 'protocol'])
        tools: Allowed tools (e.g., ['Read', 'Edit', 'Grep', 'Bash'])
        project: Project name (optional, NULL = global agent)
    """
    app = ctx.request_context.lifespan_context

    # Check for duplicate
    existing = await app.db.fetchrow(
        "SELECT id FROM agent_specs WHERE name = $1", name
    )
    if existing:
        return json.dumps({
            "success": False,
            "agent_id": existing["id"],
            "message": f"Agent '{name}' already exists with id {existing['id']}. Use update_agent to modify."
        })

    # Resolve project
    project_id = None
    if project:
        project_id = await resolve_project_id(app.db, project)
        if not project_id:
            return json.dumps({"error": f"Project '{project}' not found"})

    # Generate embedding from summary (or description if no summary)
    embedding_text = summary or description
    embedding = await get_embedding(app.openai, embedding_text)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO agent_specs (name, description, spec_content, summary, model,
                                 triggers, tools, project_id, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector)
        RETURNING id
        """,
        name, description, spec_content, summary, model,
        triggers or [], tools or [], project_id, embedding_str
    )

    return json.dumps({
        "success": True,
        "agent_id": row["id"],
        "message": f"Agent '{name}' registered successfully"
    })


@mcp.tool()
async def get_agent(name: str, ctx: Context = None) -> str:
    """
    Get full agent specification by name. Returns the complete spec_content
    for Claude to follow as inline instructions.

    Args:
        name: Agent identifier
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        SELECT a.*, p.name as project_name
        FROM agent_specs a
        LEFT JOIN projects p ON a.project_id = p.id
        WHERE a.name = $1 AND a.retired_at IS NULL
        """,
        name
    )

    if not row:
        return json.dumps({"error": f"Agent '{name}' not found or is retired"})

    return json.dumps({
        "name": row["name"],
        "description": row["description"],
        "spec_content": row["spec_content"],
        "summary": row["summary"],
        "model": row["model"],
        "triggers": row["triggers"],
        "tools": row["tools"],
        "project": row["project_name"],
        "version": row["version"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
    })


@mcp.tool()
async def update_agent(
    agent_id: int,
    description: str = None,
    spec_content: str = None,
    summary: str = None,
    model: str = None,
    triggers: list[str] = None,
    tools: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Update an existing agent specification. Only provided fields are changed.
    Auto-increments version. Regenerates embedding if summary changes.

    Args:
        agent_id: ID of the agent to update
        description: New description
        spec_content: New full specification content
        summary: New summary (triggers re-embedding)
        model: New preferred model
        triggers: New trigger keywords
        tools: New allowed tools
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT * FROM agent_specs WHERE id = $1", agent_id
    )
    if not existing:
        return json.dumps({"error": f"Agent {agent_id} not found"})

    updates = ["version = version + 1", "updated_at = NOW()"]
    params = []
    param_idx = 1

    if description is not None:
        updates.append(f"description = ${param_idx}")
        params.append(description)
        param_idx += 1

    if spec_content is not None:
        updates.append(f"spec_content = ${param_idx}")
        params.append(spec_content)
        param_idx += 1

    if summary is not None:
        updates.append(f"summary = ${param_idx}")
        params.append(summary)
        param_idx += 1

    if model is not None:
        updates.append(f"model = ${param_idx}")
        params.append(model)
        param_idx += 1

    if triggers is not None:
        updates.append(f"triggers = ${param_idx}")
        params.append(triggers)
        param_idx += 1

    if tools is not None:
        updates.append(f"tools = ${param_idx}")
        params.append(tools)
        param_idx += 1

    if len(params) == 0:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if summary or description changed
    if summary is not None or description is not None:
        new_summary = summary if summary is not None else existing["summary"]
        new_description = description if description is not None else existing["description"]
        embed_text = new_summary or new_description
        embedding = await get_embedding(app.openai, embed_text)
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(agent_id)
    await app.db.execute(
        f"UPDATE agent_specs SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({
        "success": True,
        "message": f"Agent {agent_id} ('{existing['name']}') updated"
    })


@mcp.tool()
async def list_agents(
    project: str = None,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    List agent specifications. Returns summary-level info (not full spec_content).

    Args:
        project: Filter by project name (optional)
        include_retired: Include retired agents (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []
    param_idx = 1

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            # Include both project-specific and global agents
            conditions.append(f"(a.project_id = ${param_idx} OR a.project_id IS NULL)")
            params.append(project_id)
            param_idx += 1

    if not include_retired:
        conditions.append("a.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    rows = await app.db.fetch(
        f"""
        SELECT a.id, a.name, a.description, a.summary, a.model,
               a.triggers, a.tools, a.version, a.project_id,
               p.name as project_name, a.retired_at
        FROM agent_specs a
        LEFT JOIN projects p ON a.project_id = p.id
        WHERE {where_clause}
        ORDER BY a.name
        """,
        *params
    )

    agents = []
    for row in rows:
        agents.append({
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "summary": row["summary"],
            "model": row["model"],
            "triggers": row["triggers"],
            "tools": row["tools"],
            "version": row["version"],
            "project": row["project_name"],
            "retired": row["retired_at"] is not None
        })

    return json.dumps({"agents": agents})


@mcp.tool()
async def retire_agent(
    agent_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire an agent specification (soft delete).
    Retired agents are excluded from suggest_agent and list_agents by default.

    Args:
        agent_id: ID of the agent to retire
        reason: Why the agent is being retired
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, name FROM agent_specs WHERE id = $1", agent_id
    )
    if not existing:
        return json.dumps({"error": f"Agent {agent_id} not found"})

    await app.db.execute(
        "UPDATE agent_specs SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, agent_id
    )

    return json.dumps({
        "success": True,
        "message": f"Agent {agent_id} ('{existing['name']}') retired"
    })


@mcp.tool()
async def suggest_agent(
    task_description: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Suggest which agent to use for a task. Combines semantic similarity
    on embeddings with keyword matching on triggers.

    Args:
        task_description: Description of the task you're about to do
        project: Project name to include project-specific agents
    """
    app = ctx.request_context.lifespan_context

    # Generate embedding for semantic search
    embedding = await get_embedding(app.openai, task_description)
    embedding_str = format_embedding(embedding)

    # Build project filter
    project_filter = "AND a.retired_at IS NULL"
    params = [embedding_str]
    param_idx = 2

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            project_filter += f" AND (a.project_id = ${param_idx} OR a.project_id IS NULL)"
            params.append(project_id)
            param_idx += 1

    # Semantic search
    rows = await app.db.fetch(
        f"""
        SELECT a.id, a.name, a.description, a.summary, a.model,
               a.triggers, a.tools, p.name as project_name,
               1 - (a.embedding <=> $1::vector) as similarity
        FROM agent_specs a
        LEFT JOIN projects p ON a.project_id = p.id
        WHERE a.embedding IS NOT NULL {project_filter}
        ORDER BY similarity DESC
        LIMIT 10
        """,
        *params
    )

    # Score: combine semantic similarity with trigger keyword matching
    task_lower = task_description.lower()
    task_words = set(task_lower.split())
    scored = []

    for row in rows:
        semantic_score = float(row["similarity"])
        trigger_score = 0.0
        matched_triggers = []

        for trigger in (row["triggers"] or []):
            if trigger.lower() in task_lower:
                trigger_score += 1.0
                matched_triggers.append(trigger)
            elif trigger.lower() in task_words:
                trigger_score += 0.5
                matched_triggers.append(trigger)

        # Weighted combination: 60% semantic, 40% trigger
        combined = (semantic_score * 0.6) + (min(trigger_score, 3.0) / 3.0 * 0.4)

        scored.append({
            "name": row["name"],
            "description": row["description"],
            "model": row["model"],
            "project": row["project_name"],
            "semantic_similarity": round(semantic_score, 3),
            "matched_triggers": matched_triggers,
            "combined_score": round(combined, 3)
        })

    scored.sort(key=lambda x: x["combined_score"], reverse=True)
    top_3 = scored[:3]

    # Determine confidence
    top_score = top_3[0]["combined_score"] if top_3 else 0
    confidence = "high" if top_score >= 0.7 else "medium" if top_score >= 0.5 else "low"

    return json.dumps({
        "task": task_description,
        "recommendation": top_3[0]["name"] if top_3 else None,
        "confidence": confidence,
        "suggested_agents": top_3
    })
