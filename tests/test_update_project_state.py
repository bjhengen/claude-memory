"""update_project_state must support partial updates against an existing state row.

Bug (lesson #1382): the ON CONFLICT DO UPDATE SET clause numbered its $N
placeholders dynamically based on which optional args were passed, while
execute() always passed a fixed six-value list. The numbering only aligned
when current_focus, blockers, and next_steps were all provided — any partial
update failed with a text[]/text type error (or would have mis-assigned
values). These tests pin the partial-update paths.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.server import AppContext
from src.tools.admin import update_project_state


def _ctx(db_pool):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )
    return ctx


async def _seed(db_pool, name):
    """Project with an existing state row, so updates hit ON CONFLICT."""
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ($1) RETURNING id", name,
    )
    await db_pool.execute(
        """INSERT INTO project_state (project_id, current_focus, blockers, next_steps)
           VALUES ($1, 'original focus', ARRAY['original blocker'], ARRAY['original step'])""",
        proj["id"],
    )
    return proj["id"]


async def _cleanup(db_pool, project_id):
    await db_pool.execute("DELETE FROM project_state WHERE project_id = $1", project_id)
    await db_pool.execute("DELETE FROM projects WHERE id = $1", project_id)


async def _state(db_pool, project_id):
    return await db_pool.fetchrow(
        """SELECT current_focus, blockers, next_steps, source_agent
           FROM project_state WHERE project_id = $1""",
        project_id,
    )


@pytest.mark.asyncio
async def test_partial_update_next_steps_only(db_pool):
    pid = await _seed(db_pool, "ups-next-steps-only")
    try:
        result = await update_project_state(
            project="ups-next-steps-only",
            next_steps=["step one", "step two"],
            ctx=_ctx(db_pool),
        )
        assert json.loads(result).get("success") is True
        row = await _state(db_pool, pid)
        assert row["next_steps"] == ["step one", "step two"]
        assert row["current_focus"] == "original focus"
        assert row["blockers"] == ["original blocker"]
    finally:
        await _cleanup(db_pool, pid)


@pytest.mark.asyncio
async def test_partial_update_current_focus_only(db_pool):
    pid = await _seed(db_pool, "ups-focus-only")
    try:
        result = await update_project_state(
            project="ups-focus-only",
            current_focus="new focus",
            ctx=_ctx(db_pool),
        )
        assert json.loads(result).get("success") is True
        row = await _state(db_pool, pid)
        assert row["current_focus"] == "new focus"
        assert row["blockers"] == ["original blocker"]
        assert row["next_steps"] == ["original step"]
        # v6 stamping must land in the text column, not a mis-numbered array
        assert row["source_agent"] == "claude"
    finally:
        await _cleanup(db_pool, pid)


@pytest.mark.asyncio
async def test_partial_update_blockers_only(db_pool):
    pid = await _seed(db_pool, "ups-blockers-only")
    try:
        result = await update_project_state(
            project="ups-blockers-only",
            blockers=["waiting on review"],
            ctx=_ctx(db_pool),
        )
        assert json.loads(result).get("success") is True
        row = await _state(db_pool, pid)
        assert row["blockers"] == ["waiting on review"]
        assert row["current_focus"] == "original focus"
        assert row["next_steps"] == ["original step"]
    finally:
        await _cleanup(db_pool, pid)


@pytest.mark.asyncio
async def test_full_update_still_works(db_pool):
    pid = await _seed(db_pool, "ups-full-update")
    try:
        result = await update_project_state(
            project="ups-full-update",
            current_focus="full focus",
            blockers=["full blocker"],
            next_steps=["full step"],
            ctx=_ctx(db_pool),
        )
        assert json.loads(result).get("success") is True
        row = await _state(db_pool, pid)
        assert row["current_focus"] == "full focus"
        assert row["blockers"] == ["full blocker"]
        assert row["next_steps"] == ["full step"]
    finally:
        await _cleanup(db_pool, pid)


@pytest.mark.asyncio
async def test_partial_insert_creates_state_row(db_pool):
    """No existing state row: a partial call inserts with defaults for the rest."""
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('ups-fresh-insert') RETURNING id",
    )
    pid = proj["id"]
    try:
        result = await update_project_state(
            project="ups-fresh-insert",
            next_steps=["first step"],
            ctx=_ctx(db_pool),
        )
        assert json.loads(result).get("success") is True
        row = await _state(db_pool, pid)
        assert row["next_steps"] == ["first step"]
        assert row["current_focus"] == ""
        assert row["blockers"] == []
    finally:
        await _cleanup(db_pool, pid)


@pytest.mark.asyncio
async def test_no_fields_returns_error(db_pool):
    pid = await _seed(db_pool, "ups-no-fields")
    try:
        result = await update_project_state(
            project="ups-no-fields",
            ctx=_ctx(db_pool),
        )
        assert "error" in json.loads(result)
    finally:
        await _cleanup(db_pool, pid)
