"""Admin tools: update_project_state, check_guardrails, add_project, get_permissions."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.helpers import resolve_project_id


@mcp.tool()
async def update_project_state(
    project: str,
    current_focus: str = None,
    blockers: list[str] = None,
    next_steps: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Update the current state of a project.

    Args:
        project: Project name
        current_focus: What we're currently working on
        blockers: Things we're stuck on
        next_steps: What to do next
    """
    app = ctx.request_context.lifespan_context

    # Get project ID
    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    # Build update
    updates = []
    params = [project_id]
    param_idx = 2

    if current_focus is not None:
        updates.append(f"current_focus = ${param_idx}")
        params.append(current_focus)
        param_idx += 1

    if blockers is not None:
        updates.append(f"blockers = ${param_idx}")
        params.append(blockers)
        param_idx += 1

    if next_steps is not None:
        updates.append(f"next_steps = ${param_idx}")
        params.append(next_steps)
        param_idx += 1

    if not updates:
        return json.dumps({"error": "No updates provided"})

    updates.append("updated_at = NOW()")

    await app.db.execute(
        f"""
        INSERT INTO project_state (project_id, current_focus, blockers, next_steps)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (project_id) DO UPDATE SET {', '.join(updates)}
        """,
        project_id,
        current_focus or "",
        blockers or [],
        next_steps or []
    )

    return json.dumps({"success": True, "message": f"State updated for {project}"})


@mcp.tool()
async def check_guardrails(
    project: str,
    action: str,
    ctx: Context = None
) -> str:
    """
    Check for any guardrails that apply before taking an action.

    Args:
        project: Project name
        action: What action you're about to take (e.g., 'build', 'deploy', 'push')
    """
    app = ctx.request_context.lifespan_context

    # Get project ID
    project_id = await resolve_project_id(app.db, project)

    # Get applicable guardrails
    guardrails = await app.db.fetch(
        """
        SELECT * FROM guardrails
        WHERE (project_id = $1 OR project_id IS NULL)
          AND (check_type = 'always' OR check_type = $2)
        ORDER BY severity
        """,
        project_id, action
    )

    if not guardrails:
        return json.dumps({"guardrails": [], "message": "No guardrails apply"})

    result = [
        {
            "description": g["description"],
            "file_path": g["file_path"],
            "pattern": g["pattern"],
            "severity": g["severity"]
        }
        for g in guardrails
    ]

    critical = [g for g in result if g["severity"] == "critical"]

    return json.dumps({
        "guardrails": result,
        "has_critical": len(critical) > 0,
        "message": f"Found {len(result)} guardrails ({len(critical)} critical)"
    })


@mcp.tool()
async def add_project(
    name: str,
    path: str,
    machine: str = None,
    tech_stack: dict = None,
    status: str = "active",
    ctx: Context = None
) -> str:
    """
    Register a new project.

    Args:
        name: Project name
        path: Path on the machine
        machine: Primary development machine
        tech_stack: Technology stack as dict
        status: Project status (active, production, inactive)
    """
    app = ctx.request_context.lifespan_context

    # Get machine ID
    machine_id = None
    if machine:
        machine_row = await app.db.fetchrow("SELECT id FROM machines WHERE name = $1", machine)
        if machine_row:
            machine_id = machine_row["id"]

    row = await app.db.fetchrow(
        """
        INSERT INTO projects (name, path, machine_id, tech_stack, status)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (name) DO UPDATE SET
            path = $2,
            machine_id = COALESCE($3, projects.machine_id),
            tech_stack = COALESCE($4, projects.tech_stack),
            status = $5,
            updated_at = NOW()
        RETURNING id
        """,
        name, path, machine_id, json.dumps(tech_stack or {}), status
    )

    return json.dumps({"success": True, "project_id": row["id"]})


@mcp.tool()
async def get_permissions(scope: str = "global", ctx: Context = None) -> str:
    """
    Get permissions for a scope.

    Args:
        scope: 'global' or 'project:name'
    """
    app = ctx.request_context.lifespan_context

    rows = await app.db.fetch(
        "SELECT * FROM permissions WHERE scope = $1 OR scope = 'global' ORDER BY action_type",
        scope
    )

    permissions = [
        {
            "action_type": r["action_type"],
            "pattern": r["pattern"],
            "allowed": r["allowed"],
            "requires_confirmation": r["requires_confirmation"],
            "notes": r["notes"]
        }
        for r in rows
    ]

    return json.dumps({"permissions": permissions})


@mcp.tool()
async def merge_projects(
    keep: str,
    merge: str,
    ctx: Context = None
) -> str:
    """
    Merge one project into another. Reassigns all associated data
    (lessons, sessions, journal entries, key_files, approaches, state)
    from the merge project to the keep project. Creates an alias so
    the old name still resolves. Deletes the merge project record.

    Args:
        keep: Project name to keep (canonical)
        merge: Project name to merge into keep (will be deleted)
    """
    app = ctx.request_context.lifespan_context

    keep_row = await app.db.fetchrow(
        "SELECT id, name FROM projects WHERE LOWER(name) = LOWER($1)", keep
    )
    merge_row = await app.db.fetchrow(
        "SELECT id, name FROM projects WHERE LOWER(name) = LOWER($1)", merge
    )

    if not keep_row:
        return json.dumps({"error": f"Keep project '{keep}' not found"})
    if not merge_row:
        return json.dumps({"error": f"Merge project '{merge}' not found"})
    if keep_row["id"] == merge_row["id"]:
        return json.dumps({"error": "Cannot merge a project into itself"})

    keep_id = keep_row["id"]
    merge_id = merge_row["id"]
    moved = {}

    # Reassign lessons
    result = await app.db.execute(
        "UPDATE lessons SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["lessons"] = int(result.split()[-1])

    # Reassign sessions
    result = await app.db.execute(
        "UPDATE sessions SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["sessions"] = int(result.split()[-1])

    # Reassign journal entries
    result = await app.db.execute(
        "UPDATE journal SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["journal_entries"] = int(result.split()[-1])

    # Reassign key_files
    result = await app.db.execute(
        "UPDATE key_files SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["key_files"] = int(result.split()[-1])

    # Reassign approaches
    result = await app.db.execute(
        "UPDATE approaches SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["approaches"] = int(result.split()[-1])

    # Delete merge project's state (keep project's state wins)
    await app.db.execute("DELETE FROM project_state WHERE project_id = $1", merge_id)

    # Create alias for old name
    await app.db.execute(
        """
        INSERT INTO project_aliases (alias, project_id)
        VALUES ($1, $2)
        ON CONFLICT (alias) DO NOTHING
        """,
        merge_row["name"], keep_id
    )

    # Delete merge project
    await app.db.execute("DELETE FROM projects WHERE id = $1", merge_id)

    return json.dumps({
        "success": True,
        "message": f"Merged '{merge_row['name']}' into '{keep_row['name']}'",
        "moved": moved,
        "alias_created": merge_row["name"]
    })
