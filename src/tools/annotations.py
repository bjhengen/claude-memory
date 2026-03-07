"""Annotation tools: annotate, get_annotations, clear_annotation."""

import json
from datetime import datetime, timezone

from mcp.server.fastmcp import Context

from src.server import mcp


VALID_ENTITY_TYPES = {"lesson", "spec", "agent", "project", "mcp_server", "mcp_tool"}


@mcp.tool()
async def annotate(
    entity_type: str,
    entity_id: int,
    note: str,
    ctx: Context = None
) -> str:
    """
    Add or append an annotation to any entity.

    If an annotation already exists for this entity, the new note is appended
    with a timestamp separator. Use for corrections, tips, or additional context.

    Args:
        entity_type: Type of entity (lesson, spec, agent, project, mcp_server, mcp_tool)
        entity_id: ID of the entity to annotate
        note: The annotation text to add
    """
    app = ctx.request_context.lifespan_context

    if entity_type not in VALID_ENTITY_TYPES:
        return json.dumps({
            "error": f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(sorted(VALID_ENTITY_TYPES))}"
        })

    # Check for existing annotation
    existing = await app.db.fetchrow(
        "SELECT id, note FROM annotations WHERE entity_type = $1 AND entity_id = $2",
        entity_type, entity_id
    )

    if existing:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        combined_note = f"{existing['note']}\n\n---\n[{timestamp}] {note}"
        await app.db.execute(
            "UPDATE annotations SET note = $1, updated_at = NOW() WHERE id = $2",
            combined_note, existing["id"]
        )
        return json.dumps({
            "success": True,
            "annotation_id": existing["id"],
            "message": f"Appended to existing annotation {existing['id']} on {entity_type} {entity_id}"
        })

    row = await app.db.fetchrow(
        """
        INSERT INTO annotations (entity_type, entity_id, note)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        entity_type, entity_id, note
    )

    return json.dumps({
        "success": True,
        "annotation_id": row["id"],
        "message": f"Annotation created for {entity_type} {entity_id}"
    })


@mcp.tool()
async def get_annotations(
    entity_type: str,
    entity_id: int,
    ctx: Context = None
) -> str:
    """
    Retrieve all annotations for an entity.

    Args:
        entity_type: Type of entity (lesson, spec, agent, project, mcp_server, mcp_tool)
        entity_id: ID of the entity
    """
    app = ctx.request_context.lifespan_context

    if entity_type not in VALID_ENTITY_TYPES:
        return json.dumps({
            "error": f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(sorted(VALID_ENTITY_TYPES))}"
        })

    rows = await app.db.fetch(
        "SELECT id, note, created_at, updated_at FROM annotations "
        "WHERE entity_type = $1 AND entity_id = $2 ORDER BY created_at",
        entity_type, entity_id
    )

    annotations = [
        {
            "id": r["id"],
            "note": r["note"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat()
        }
        for r in rows
    ]

    return json.dumps({
        "entity_type": entity_type,
        "entity_id": entity_id,
        "annotations": annotations,
        "count": len(annotations)
    })


@mcp.tool()
async def clear_annotation(
    annotation_id: int,
    ctx: Context = None
) -> str:
    """
    Delete an annotation by its ID.

    Args:
        annotation_id: ID of the annotation to delete
    """
    app = ctx.request_context.lifespan_context

    result = await app.db.execute(
        "DELETE FROM annotations WHERE id = $1",
        annotation_id
    )

    if result == "DELETE 1":
        return json.dumps({
            "success": True,
            "message": f"Annotation {annotation_id} deleted"
        })

    return json.dumps({
        "success": False,
        "message": f"Annotation {annotation_id} not found"
    })
