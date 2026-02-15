"""Project tools: get_project, list_projects."""

import json
import re

from mcp.server.fastmcp import Context

from src.helpers import resolve_project_id
from src.server import mcp


@mcp.tool()
async def get_project(name: str, ctx: Context = None) -> str:
    """
    Get full context for a project including current state, approaches, and key files.

    Args:
        name: Project name (e.g., 'recipe.sync', 'wine.dine Pro')
    """
    app = ctx.request_context.lifespan_context

    # Resolve project ID (case-insensitive, alias-aware)
    project_id = await resolve_project_id(app.db, name)
    if not project_id:
        return json.dumps({"error": f"Project '{name}' not found"})

    # Get project
    project = await app.db.fetchrow(
        """
        SELECT p.*, m.name as machine_name, m.ssh_command
        FROM projects p
        LEFT JOIN machines m ON p.machine_id = m.id
        WHERE p.id = $1
        """,
        project_id
    )

    # Get current approaches
    approaches = await app.db.fetch(
        "SELECT * FROM approaches WHERE project_id = $1 AND status = 'current'",
        project_id
    )

    # Get key files
    key_files = await app.db.fetch(
        "SELECT * FROM key_files WHERE project_id = $1 ORDER BY importance",
        project_id
    )

    # Get current state
    state = await app.db.fetchrow(
        "SELECT * FROM project_state WHERE project_id = $1",
        project_id
    )

    # Get guardrails
    guardrails = await app.db.fetch(
        "SELECT * FROM guardrails WHERE project_id = $1 OR project_id IS NULL",
        project_id
    )

    result = {
        "project": {
            "name": project["name"],
            "path": project["path"],
            "machine": project["machine_name"],
            "ssh_command": project["ssh_command"],
            "status": project["status"],
            "tech_stack": project["tech_stack"],
            "current_phase": project["current_phase"],
            "updated_at": project["updated_at"].isoformat() if project["updated_at"] else None
        },
        "approaches": [
            {
                "area": a["area"],
                "current": a["current_approach"],
                "previous": a["previous_approach"],
                "reason": a["reason_for_change"]
            }
            for a in approaches
        ],
        "key_files": [
            {
                "path": f["file_path"],
                "line": f["line_hint"],
                "description": f["description"],
                "importance": f["importance"]
            }
            for f in key_files
        ],
        "state": {
            "current_focus": state["current_focus"] if state else None,
            "blockers": state["blockers"] if state else [],
            "next_steps": state["next_steps"] if state else []
        } if state else None,
        "guardrails": [
            {
                "description": g["description"],
                "check_type": g["check_type"],
                "file_path": g["file_path"],
                "severity": g["severity"]
            }
            for g in guardrails
        ]
    }

    return json.dumps(result)


@mcp.tool()
async def list_projects(status: str = None, ctx: Context = None) -> str:
    """
    List all projects, optionally filtered by status.

    Args:
        status: Filter by status (active, production, inactive)
    """
    app = ctx.request_context.lifespan_context

    if status:
        rows = await app.db.fetch(
            "SELECT name, path, status, current_phase FROM projects WHERE status = $1",
            status
        )
    else:
        rows = await app.db.fetch(
            "SELECT name, path, status, current_phase FROM projects ORDER BY name"
        )

    projects = [
        {
            "name": r["name"],
            "path": r["path"],
            "status": r["status"],
            "current_phase": r["current_phase"]
        }
        for r in rows
    ]

    return json.dumps({"projects": projects})


@mcp.tool()
async def get_project_claude_md(project: str, ctx: Context = None) -> str:
    """
    Get the CLAUDE.md content for a project.
    Returns the project's philosophy, scope, conventions, and active context.

    Args:
        project: Project name
    """
    app = ctx.request_context.lifespan_context
    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    row = await app.db.fetchrow(
        "SELECT name, claude_md FROM projects WHERE id = $1",
        project_id
    )

    return json.dumps({
        "project": row["name"],
        "claude_md": row["claude_md"],
        "has_claude_md": row["claude_md"] is not None
    })


@mcp.tool()
async def set_project_claude_md(project: str, content: str, ctx: Context = None) -> str:
    """
    Create or fully replace the CLAUDE.md content for a project.

    Args:
        project: Project name
        content: Full CLAUDE.md content (markdown)
    """
    app = ctx.request_context.lifespan_context
    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    await app.db.execute(
        "UPDATE projects SET claude_md = $1, updated_at = NOW() WHERE id = $2",
        content, project_id
    )

    return json.dumps({"success": True, "message": f"CLAUDE.md set for {project}"})


@mcp.tool()
async def update_project_claude_md(
    project: str,
    section: str,
    content: str,
    ctx: Context = None
) -> str:
    """
    Update a specific section of a project's CLAUDE.md by heading match.
    If the section exists, replaces it. If not, appends it.

    Args:
        project: Project name
        section: Section heading to find/replace (e.g., "## Architecture")
        content: New content for this section (include the heading)
    """
    app = ctx.request_context.lifespan_context
    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    row = await app.db.fetchrow(
        "SELECT claude_md FROM projects WHERE id = $1",
        project_id
    )

    existing = row["claude_md"] or ""

    heading_match = re.match(r'^(#{1,6})\s', section)
    if not heading_match:
        return json.dumps({"error": "Section must start with a markdown heading (e.g., '## Architecture')"})

    level = len(heading_match.group(1))
    pattern = re.compile(
        rf'^{re.escape(section)}.*?(?=^#{{1,{level}}}\s|\Z)',
        re.MULTILINE | re.DOTALL
    )

    if pattern.search(existing):
        updated = pattern.sub(content.rstrip() + '\n\n', existing, count=1)
        action = "updated"
    else:
        updated = existing.rstrip() + '\n\n' + content.rstrip() + '\n'
        action = "appended"

    await app.db.execute(
        "UPDATE projects SET claude_md = $1, updated_at = NOW() WHERE id = $2",
        updated, project_id
    )

    return json.dumps({"success": True, "message": f"Section '{section}' {action} in CLAUDE.md for {project}"})
