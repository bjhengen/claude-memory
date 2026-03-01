"""MCP server registry tools for cataloging servers, tools, and capabilities."""

import json
from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def register_mcp_server(
    name: str,
    description: str,
    transport: str,
    url: str = None,
    machine: str = None,
    auth_type: str = "none",
    auth_hint: str = None,
    config_snippet: dict = None,
    limitations: str = None,
    projects: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Register an MCP server in the catalog.

    Args:
        name: Server identifier (e.g., 'claude-memory', 'context7')
        description: What this server provides
        transport: Connection type: 'stdio', 'sse', 'streamable-http'
        url: Connection endpoint (NULL for stdio)
        machine: Machine name where it runs (NULL = cloud/anywhere)
        auth_type: Authentication: 'api-key', 'oauth', 'none'
        auth_hint: How to authenticate (do not store secrets)
        config_snippet: Example config JSON for settings files
        limitations: Runtime constraints, rate limits, machine requirements
        projects: List of project names this server serves
    """
    app = ctx.request_context.lifespan_context

    # Check for duplicate
    existing = await app.db.fetchrow(
        "SELECT id FROM mcp_servers WHERE name = $1", name
    )
    if existing:
        return json.dumps({
            "success": False,
            "server_id": existing["id"],
            "message": f"Server '{name}' already exists with id {existing['id']}. Use update_mcp_server to modify."
        })

    # Resolve machine
    machine_id = None
    if machine:
        machine_row = await app.db.fetchrow(
            "SELECT id FROM machines WHERE name = $1", machine
        )
        if machine_row:
            machine_id = machine_row["id"]

    # Generate embedding
    embedding = await get_embedding(app.openai, description)
    embedding_str = format_embedding(embedding)

    # Convert config_snippet to JSON string for JSONB
    config_json = json.dumps(config_snippet) if config_snippet else None

    row = await app.db.fetchrow(
        """
        INSERT INTO mcp_servers (name, description, url, transport, machine_id,
                                 auth_type, auth_hint, config_snippet, limitations, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::vector)
        RETURNING id
        """,
        name, description, url, transport, machine_id,
        auth_type, auth_hint, config_json, limitations, embedding_str
    )

    server_id = row["id"]

    # Link to projects
    if projects:
        for proj_name in projects:
            project_id = await resolve_project_id(app.db, proj_name)
            if project_id:
                await app.db.execute(
                    """
                    INSERT INTO mcp_server_projects (server_id, project_id)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    """,
                    server_id, project_id
                )

    return json.dumps({
        "success": True,
        "server_id": server_id,
        "message": f"MCP server '{name}' registered"
    })


@mcp.tool()
async def get_mcp_server(name: str, ctx: Context = None) -> str:
    """
    Get full MCP server info including all its tools.

    Args:
        name: Server identifier
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        SELECT s.*, m.name as machine_name, m.ip as machine_ip,
               m.ssh_command as machine_ssh
        FROM mcp_servers s
        LEFT JOIN machines m ON s.machine_id = m.id
        WHERE s.name = $1
        """,
        name
    )

    if not row:
        return json.dumps({"error": f"MCP server '{name}' not found"})

    # Get tools
    tools = await app.db.fetch(
        """
        SELECT tool_name, description, parameters
        FROM mcp_server_tools
        WHERE server_id = $1
        ORDER BY tool_name
        """,
        row["id"]
    )

    # Get linked projects
    projects = await app.db.fetch(
        """
        SELECT p.name
        FROM mcp_server_projects sp
        JOIN projects p ON sp.project_id = p.id
        WHERE sp.server_id = $1
        """,
        row["id"]
    )

    return json.dumps({
        "name": row["name"],
        "description": row["description"],
        "url": row["url"],
        "transport": row["transport"],
        "machine": row["machine_name"],
        "machine_ip": row["machine_ip"],
        "machine_ssh": row["machine_ssh"],
        "auth_type": row["auth_type"],
        "auth_hint": row["auth_hint"],
        "config_snippet": row["config_snippet"],
        "limitations": row["limitations"],
        "status": row["status"],
        "retired": row["retired_at"] is not None,
        "tools": [
            {
                "name": t["tool_name"],
                "description": t["description"],
                "parameters": t["parameters"]
            }
            for t in tools
        ],
        "projects": [p["name"] for p in projects],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
    })


@mcp.tool()
async def update_mcp_server(
    server_id: int,
    description: str = None,
    url: str = None,
    transport: str = None,
    machine: str = None,
    auth_type: str = None,
    auth_hint: str = None,
    config_snippet: dict = None,
    limitations: str = None,
    status: str = None,
    ctx: Context = None
) -> str:
    """
    Update an MCP server registration. Only provided fields are changed.

    Args:
        server_id: ID of the server to update
        description: New description (triggers re-embedding)
        url: New connection URL
        transport: New transport type
        machine: New machine name
        auth_type: New auth type
        auth_hint: New auth hint
        config_snippet: New config JSON
        limitations: New limitations text
        status: New status: 'active', 'deprecated', 'offline'
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT * FROM mcp_servers WHERE id = $1", server_id
    )
    if not existing:
        return json.dumps({"error": f"MCP server {server_id} not found"})

    updates = ["updated_at = NOW()"]
    params = []
    param_idx = 1

    if description is not None:
        updates.append(f"description = ${param_idx}")
        params.append(description)
        param_idx += 1

    if url is not None:
        updates.append(f"url = ${param_idx}")
        params.append(url)
        param_idx += 1

    if transport is not None:
        updates.append(f"transport = ${param_idx}")
        params.append(transport)
        param_idx += 1

    if machine is not None:
        machine_row = await app.db.fetchrow(
            "SELECT id FROM machines WHERE name = $1", machine
        )
        if machine_row:
            updates.append(f"machine_id = ${param_idx}")
            params.append(machine_row["id"])
            param_idx += 1

    if auth_type is not None:
        updates.append(f"auth_type = ${param_idx}")
        params.append(auth_type)
        param_idx += 1

    if auth_hint is not None:
        updates.append(f"auth_hint = ${param_idx}")
        params.append(auth_hint)
        param_idx += 1

    if config_snippet is not None:
        updates.append(f"config_snippet = ${param_idx}::jsonb")
        params.append(json.dumps(config_snippet))
        param_idx += 1

    if limitations is not None:
        updates.append(f"limitations = ${param_idx}")
        params.append(limitations)
        param_idx += 1

    if status is not None:
        updates.append(f"status = ${param_idx}")
        params.append(status)
        param_idx += 1

    if len(params) == 0:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if description changed
    if description is not None:
        embedding = await get_embedding(app.openai, description)
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(server_id)
    await app.db.execute(
        f"UPDATE mcp_servers SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({
        "success": True,
        "message": f"MCP server {server_id} ('{existing['name']}') updated"
    })


@mcp.tool()
async def retire_mcp_server(
    server_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire an MCP server (soft delete).

    Args:
        server_id: ID of the server to retire
        reason: Why the server is being retired
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, name FROM mcp_servers WHERE id = $1", server_id
    )
    if not existing:
        return json.dumps({"error": f"MCP server {server_id} not found"})

    await app.db.execute(
        "UPDATE mcp_servers SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, server_id
    )

    return json.dumps({
        "success": True,
        "message": f"MCP server {server_id} ('{existing['name']}') retired"
    })


@mcp.tool()
async def list_mcp_servers(
    machine: str = None,
    project: str = None,
    status: str = None,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    List MCP servers with optional filters.

    Args:
        machine: Filter by machine name
        project: Filter by project name
        status: Filter by status: 'active', 'deprecated', 'offline'
        include_retired: Include retired servers (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []
    param_idx = 1
    joins = ""

    if machine:
        conditions.append(f"m.name = ${param_idx}")
        params.append(machine)
        param_idx += 1

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            joins = "JOIN mcp_server_projects sp ON s.id = sp.server_id"
            conditions.append(f"sp.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if status:
        conditions.append(f"s.status = ${param_idx}")
        params.append(status)
        param_idx += 1

    if not include_retired:
        conditions.append("s.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    rows = await app.db.fetch(
        f"""
        SELECT DISTINCT s.id, s.name, s.description, s.url, s.transport,
               s.auth_type, s.status, s.limitations,
               m.name as machine_name, s.retired_at,
               (SELECT COUNT(*) FROM mcp_server_tools WHERE server_id = s.id) as tool_count
        FROM mcp_servers s
        LEFT JOIN machines m ON s.machine_id = m.id
        {joins}
        WHERE {where_clause}
        ORDER BY s.name
        """,
        *params
    )

    servers = []
    for row in rows:
        servers.append({
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "url": row["url"],
            "transport": row["transport"],
            "auth_type": row["auth_type"],
            "status": row["status"],
            "limitations": row["limitations"],
            "machine": row["machine_name"],
            "tool_count": row["tool_count"],
            "retired": row["retired_at"] is not None
        })

    return json.dumps({"servers": servers})


@mcp.tool()
async def register_mcp_tool(
    server: str,
    tool_name: str,
    description: str,
    parameters: dict = None,
    ctx: Context = None
) -> str:
    """
    Register a tool in an MCP server's catalog. Idempotent — updates if
    tool already exists for that server.

    Args:
        server: MCP server name
        tool_name: Tool identifier (e.g., 'search', 'log_lesson')
        description: What the tool does
        parameters: Parameter schema as JSON object
    """
    app = ctx.request_context.lifespan_context

    server_row = await app.db.fetchrow(
        "SELECT id FROM mcp_servers WHERE name = $1", server
    )
    if not server_row:
        return json.dumps({"error": f"MCP server '{server}' not found"})

    # Generate embedding
    embedding = await get_embedding(app.openai, description)
    embedding_str = format_embedding(embedding)

    params_json = json.dumps(parameters) if parameters else None

    row = await app.db.fetchrow(
        """
        INSERT INTO mcp_server_tools (server_id, tool_name, description, parameters, embedding)
        VALUES ($1, $2, $3, $4::jsonb, $5::vector)
        ON CONFLICT (server_id, tool_name) DO UPDATE SET
            description = EXCLUDED.description,
            parameters = EXCLUDED.parameters,
            embedding = EXCLUDED.embedding
        RETURNING id
        """,
        server_row["id"], tool_name, description, params_json, embedding_str
    )

    return json.dumps({
        "success": True,
        "tool_id": row["id"],
        "message": f"Tool '{tool_name}' registered for server '{server}'"
    })


@mcp.tool()
async def find_mcp_tools(
    query: str,
    project: str = None,
    machine: str = None,
    limit: int = 5,
    ctx: Context = None
) -> str:
    """
    Search for MCP tools across all servers by semantic similarity.
    Answers 'where can I do X?' across the whole MCP ecosystem.

    Args:
        query: What capability you're looking for (e.g., 'search documents')
        project: Filter to servers that serve this project
        machine: Filter to servers on this machine
        limit: Maximum results (default 5)
    """
    app = ctx.request_context.lifespan_context

    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    conditions = [
        "t.embedding IS NOT NULL",
        "s.retired_at IS NULL"
    ]
    params = [embedding_str]
    param_idx = 2
    joins = ""

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            joins = "JOIN mcp_server_projects sp ON s.id = sp.server_id"
            conditions.append(f"sp.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if machine:
        conditions.append(f"m.name = ${param_idx}")
        params.append(machine)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    params.append(limit)
    rows = await app.db.fetch(
        f"""
        SELECT t.tool_name, t.description as tool_description, t.parameters,
               s.name as server_name, s.description as server_description,
               s.url, s.transport, s.auth_type, s.auth_hint,
               m.name as machine_name,
               1 - (t.embedding <=> $1::vector) as similarity
        FROM mcp_server_tools t
        JOIN mcp_servers s ON t.server_id = s.id
        LEFT JOIN machines m ON s.machine_id = m.id
        {joins}
        WHERE {where_clause}
        ORDER BY similarity DESC
        LIMIT ${param_idx}
        """,
        *params
    )

    results = []
    for row in rows:
        results.append({
            "tool": row["tool_name"],
            "tool_description": row["tool_description"],
            "parameters": row["parameters"],
            "server": row["server_name"],
            "server_description": row["server_description"],
            "url": row["url"],
            "transport": row["transport"],
            "auth_type": row["auth_type"],
            "auth_hint": row["auth_hint"],
            "machine": row["machine_name"],
            "similarity": round(row["similarity"], 3)
        })

    return json.dumps({"tools": results})
