"""Session tools: start_session, end_session."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def start_session(
    machine: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Begin tracking a new work session.

    Args:
        machine: Which machine this session is on
        project: Primary project for this session (optional)
    """
    app = ctx.request_context.lifespan_context

    # Get machine ID
    machine_row = await app.db.fetchrow("SELECT id FROM machines WHERE name = $1", machine)
    machine_id = machine_row["id"] if machine_row else None

    # Get project ID
    project_id = None
    if project:
        project_id = await resolve_project_id(app.db, project)

    row = await app.db.fetchrow(
        """
        INSERT INTO sessions (machine_id, project_id)
        VALUES ($1, $2)
        RETURNING id, started_at
        """,
        machine_id, project_id
    )

    return json.dumps({
        "session_id": row["id"],
        "started_at": row["started_at"].isoformat(),
        "message": "Session started"
    })


@mcp.tool()
async def end_session(
    session_id: int,
    summary: str,
    items: list[dict] = None,
    ctx: Context = None
) -> str:
    """
    Complete a session with summary and items.

    Args:
        session_id: The session ID from start_session
        summary: Brief description of what was accomplished
        items: List of items with {type, description, file_paths}
               Types: completed, in_progress, blocked, discovered
    """
    app = ctx.request_context.lifespan_context

    # Generate embedding for session
    embedding = await get_embedding(app.openai, summary)
    embedding_str = format_embedding(embedding)

    # Update session
    await app.db.execute(
        """
        UPDATE sessions
        SET ended_at = NOW(), summary = $1, embedding = $2::vector
        WHERE id = $3
        """,
        summary, embedding_str, session_id
    )

    # Insert session items
    if items:
        for item in items:
            await app.db.execute(
                """
                INSERT INTO session_items (session_id, item_type, description, file_paths)
                VALUES ($1, $2, $3, $4)
                """,
                session_id,
                item.get("type", "completed"),
                item.get("description", ""),
                item.get("file_paths", [])
            )

    # Update project state if session has a project
    session = await app.db.fetchrow("SELECT project_id FROM sessions WHERE id = $1", session_id)
    if session and session["project_id"]:
        # Find in_progress and blocked items for next_steps and blockers
        in_progress = [i["description"] for i in (items or []) if i.get("type") == "in_progress"]
        blocked = [i["description"] for i in (items or []) if i.get("type") == "blocked"]

        await app.db.execute(
            """
            INSERT INTO project_state (project_id, last_session_id, current_focus, blockers, next_steps)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (project_id) DO UPDATE SET
                last_session_id = $2,
                current_focus = $3,
                blockers = $4,
                next_steps = $5,
                updated_at = NOW()
            """,
            session["project_id"],
            session_id,
            summary[:200],
            blocked,
            in_progress
        )

    return json.dumps({
        "success": True,
        "message": "Session ended and state saved"
    })
