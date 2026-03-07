"""Lesson tools: log_lesson, log_pattern, update_lesson, retire_lesson, rate_lesson."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def log_lesson(
    title: str,
    content: str,
    project: str = None,
    tags: list[str] = None,
    severity: str = "tip",
    ctx: Context = None
) -> str:
    """
    Save a new lesson learned.

    Args:
        title: Short title for the lesson
        content: Full explanation of what was learned
        project: Associated project (optional, for cross-project lessons)
        tags: Categorization tags (e.g., ['flutter', 'ios', 'share-sheet'])
        severity: How important: critical, important, or tip
    """
    app = ctx.request_context.lifespan_context

    # Check if lesson with same title already exists
    existing = await app.db.fetchrow(
        "SELECT id FROM lessons WHERE title = $1",
        title
    )
    if existing:
        return json.dumps({
            "success": False,
            "lesson_id": existing["id"],
            "message": f"Lesson '{title}' already exists with id {existing['id']}"
        })

    # Get project ID if specified
    project_id = None
    if project:
        project_id = await resolve_project_id(app.db, project)

    # Generate embedding
    embedding_text = f"{title}\n{content}"
    embedding = await get_embedding(app.openai, embedding_text)
    embedding_str = format_embedding(embedding)

    # Insert lesson
    row = await app.db.fetchrow(
        """
        INSERT INTO lessons (title, content, project_id, tags, severity, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::vector)
        RETURNING id
        """,
        title, content, project_id, tags or [], severity, embedding_str
    )

    return json.dumps({
        "success": True,
        "lesson_id": row["id"],
        "message": f"Lesson '{title}' saved successfully"
    })


@mcp.tool()
async def log_pattern(
    name: str,
    problem: str,
    solution: str,
    code_example: str = None,
    applies_to: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Save a reusable pattern/solution.

    Args:
        name: Short name for the pattern
        problem: What problem this solves
        solution: How to solve it
        code_example: Example code (optional)
        applies_to: Technologies/contexts this applies to
    """
    app = ctx.request_context.lifespan_context

    # Check if pattern with same name already exists
    existing = await app.db.fetchrow(
        "SELECT id FROM patterns WHERE name = $1",
        name
    )
    if existing:
        return json.dumps({
            "success": False,
            "pattern_id": existing["id"],
            "message": f"Pattern '{name}' already exists with id {existing['id']}"
        })

    # Generate embedding
    embedding_text = f"{name}\n{problem}\n{solution}"
    embedding = await get_embedding(app.openai, embedding_text)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO patterns (name, problem, solution, code_example, applies_to, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::vector)
        RETURNING id
        """,
        name, problem, solution, code_example, applies_to or [], embedding_str
    )

    return json.dumps({
        "success": True,
        "pattern_id": row["id"],
        "message": f"Pattern '{name}' saved successfully"
    })


@mcp.tool()
async def update_lesson(
    lesson_id: int,
    title: str = None,
    content: str = None,
    tags: list[str] = None,
    severity: str = None,
    ctx: Context = None
) -> str:
    """
    Update an existing lesson. Only provided fields are changed.
    Regenerates embedding if title or content changes.

    Args:
        lesson_id: ID of the lesson to update
        title: New title (optional)
        content: New content (optional)
        tags: New tags (optional)
        severity: New severity (optional)
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow("SELECT * FROM lessons WHERE id = $1", lesson_id)
    if not existing:
        return json.dumps({"error": f"Lesson {lesson_id} not found"})

    updates = []
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

    if tags is not None:
        updates.append(f"tags = ${param_idx}")
        params.append(tags)
        param_idx += 1

    if severity is not None:
        updates.append(f"severity = ${param_idx}")
        params.append(severity)
        param_idx += 1

    if not updates:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if title or content changed
    if title is not None or content is not None:
        new_title = title if title is not None else existing["title"]
        new_content = content if content is not None else existing["content"]
        embedding = await get_embedding(app.openai, f"{new_title}\n{new_content}")
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(lesson_id)
    await app.db.execute(
        f"UPDATE lessons SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({"success": True, "message": f"Lesson {lesson_id} updated"})


@mcp.tool()
async def retire_lesson(
    lesson_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire a lesson (soft delete). Retired lessons are excluded from search by default.

    Args:
        lesson_id: ID of the lesson to retire
        reason: Why this lesson is being retired (optional)
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow("SELECT id, title FROM lessons WHERE id = $1", lesson_id)
    if not existing:
        return json.dumps({"error": f"Lesson {lesson_id} not found"})

    await app.db.execute(
        "UPDATE lessons SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, lesson_id
    )

    return json.dumps({
        "success": True,
        "message": f"Lesson {lesson_id} ('{existing['title']}') retired"
    })


@mcp.tool()
async def rate_lesson(
    lesson_id: int,
    rating: str,
    comment: str = None,
    ctx: Context = None
) -> str:
    """
    Rate a lesson as helpful or unhelpful. Ratings affect search ranking —
    low-rated lessons appear lower in results but are never auto-retired.

    Args:
        lesson_id: ID of the lesson to rate
        rating: 'up' or 'down'
        comment: Optional context for the rating (saved as annotation)
    """
    if rating not in ("up", "down"):
        return json.dumps({"error": "rating must be 'up' or 'down'"})

    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, title, upvotes, downvotes FROM lessons WHERE id = $1",
        lesson_id
    )
    if not existing:
        return json.dumps({"error": f"Lesson {lesson_id} not found"})

    column = "upvotes" if rating == "up" else "downvotes"
    await app.db.execute(
        f"UPDATE lessons SET {column} = COALESCE({column}, 0) + 1, last_rated_at = NOW() WHERE id = $1",
        lesson_id
    )

    new_up = (existing["upvotes"] or 0) + (1 if rating == "up" else 0)
    new_down = (existing["downvotes"] or 0) + (1 if rating == "down" else 0)

    # If comment provided, create an annotation on the lesson
    if comment:
        prefix = "upvote" if rating == "up" else "downvote"
        await app.db.execute(
            """
            INSERT INTO annotations (entity_type, entity_id, note)
            VALUES ('lesson', $1, $2)
            """,
            lesson_id, f"[{prefix}] {comment}"
        )

    return json.dumps({
        "success": True,
        "lesson_id": lesson_id,
        "title": existing["title"],
        "upvotes": new_up,
        "downvotes": new_down,
        "message": f"Lesson {lesson_id} rated '{rating}'"
    })
