"""Lesson tools: log_lesson, log_pattern."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding


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
        row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
        if row:
            project_id = row["id"]

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
