"""Shared helper functions used across tool modules."""

import asyncpg


async def resolve_project_id(pool: asyncpg.Pool, name: str) -> int | None:
    """
    Resolve a project name to its ID, checking aliases first.
    Case-insensitive matching.

    Returns project ID or None if not found.
    """
    # Check aliases first
    row = await pool.fetchrow(
        "SELECT project_id FROM project_aliases WHERE LOWER(alias) = LOWER($1)",
        name
    )
    if row:
        return row["project_id"]

    # Fall back to direct name match
    row = await pool.fetchrow(
        "SELECT id FROM projects WHERE LOWER(name) = LOWER($1)",
        name
    )
    return row["id"] if row else None
