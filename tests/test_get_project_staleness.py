"""get_project must surface how stale the project state is.

Review 2026-07-06 (P1.4): state timestamps existed but nothing computed them —
sessions relied on week-old current_focus/next_steps without noticing (this
system's descriptions rot fast; lesson #1382 item 4).
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.server import AppContext
from src.tools.projects import get_project


def _ctx(db_pool):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )
    return ctx


async def _seed(db_pool, name, state_age_days):
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ($1) RETURNING id", name)
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=state_age_days)
    await db_pool.execute(
        """INSERT INTO project_state (project_id, current_focus, updated_at)
           VALUES ($1, 'focus', $2)""",
        proj["id"], when)
    return proj["id"]


async def _cleanup(db_pool, project_id):
    await db_pool.execute("DELETE FROM project_state WHERE project_id = $1", project_id)
    await db_pool.execute("DELETE FROM projects WHERE id = $1", project_id)


@pytest.mark.asyncio
async def test_stale_state_gets_age_and_warning(db_pool):
    pid = await _seed(db_pool, "stale-30d", 30)
    try:
        payload = json.loads(await get_project(name="stale-30d", ctx=_ctx(db_pool)))
        assert payload["state"]["state_age_days"] == 30
        warning = payload["state"]["staleness_warning"]
        assert warning is not None
        assert "30" in warning
    finally:
        await _cleanup(db_pool, pid)


@pytest.mark.asyncio
async def test_fresh_state_has_no_warning(db_pool):
    pid = await _seed(db_pool, "stale-fresh", 0)
    try:
        payload = json.loads(await get_project(name="stale-fresh", ctx=_ctx(db_pool)))
        assert payload["state"]["state_age_days"] == 0
        assert payload["state"]["staleness_warning"] is None
    finally:
        await _cleanup(db_pool, pid)


@pytest.mark.asyncio
async def test_project_without_state_still_works(db_pool):
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('stale-nostate') RETURNING id")
    try:
        payload = json.loads(await get_project(name="stale-nostate", ctx=_ctx(db_pool)))
        assert payload["state"] is None
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id = $1", proj["id"])
