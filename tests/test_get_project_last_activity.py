"""get_project must surface true last-activity, not just projects.updated_at.

Background: `projects.updated_at` only bumps on the projects row itself
(creation, claude_md edits, add_project upsert). It does NOT track lessons,
journal entries, sessions, or project_state changes. The `last_activity_at`
field added in this fix computes MAX across all related tables.
"""

import json
from datetime import datetime, timedelta
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


@pytest.mark.asyncio
async def test_last_activity_at_is_max_of_lessons(db_pool):
    """A lesson logged after projects.updated_at bumps last_activity_at."""
    old = datetime(2020, 1, 1, 0, 0, 0)
    new = datetime(2099, 1, 1, 0, 0, 0)
    proj = await db_pool.fetchrow(
        """INSERT INTO projects (name, updated_at) VALUES ('v6-la-lessons', $1)
           RETURNING id""",
        old,
    )
    lesson = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, project_id, learned_at)
           VALUES ('la-test-lesson', 'c', $1, $2) RETURNING id""",
        proj["id"], new,
    )
    try:
        result = await get_project(name="v6-la-lessons", ctx=_ctx(db_pool))
        payload = json.loads(result)
        last = datetime.fromisoformat(payload["project"]["last_activity_at"])
        assert last == new
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson["id"])
        await db_pool.execute("DELETE FROM projects WHERE id = $1", proj["id"])


@pytest.mark.asyncio
async def test_last_activity_at_is_max_of_journal(db_pool):
    """A journal entry written after projects.updated_at bumps last_activity_at."""
    old = datetime(2020, 1, 1, 0, 0, 0)
    new = datetime(2099, 1, 1, 0, 0, 0)
    proj = await db_pool.fetchrow(
        """INSERT INTO projects (name, updated_at) VALUES ('v6-la-journal', $1)
           RETURNING id""",
        old,
    )
    entry = await db_pool.fetchrow(
        """INSERT INTO journal (content, project_id, entry_date)
           VALUES ('la-test-journal', $1, $2) RETURNING id""",
        proj["id"], new,
    )
    try:
        result = await get_project(name="v6-la-journal", ctx=_ctx(db_pool))
        payload = json.loads(result)
        last = datetime.fromisoformat(payload["project"]["last_activity_at"])
        assert last == new
    finally:
        await db_pool.execute("DELETE FROM journal WHERE id = $1", entry["id"])
        await db_pool.execute("DELETE FROM projects WHERE id = $1", proj["id"])


@pytest.mark.asyncio
async def test_last_activity_at_falls_back_to_projects_updated_at(db_pool):
    """When no related rows exist, last_activity_at falls back to projects.updated_at."""
    when = datetime(2055, 6, 15, 12, 0, 0)
    proj = await db_pool.fetchrow(
        """INSERT INTO projects (name, updated_at) VALUES ('v6-la-fallback', $1)
           RETURNING id""",
        when,
    )
    try:
        result = await get_project(name="v6-la-fallback", ctx=_ctx(db_pool))
        payload = json.loads(result)
        last = datetime.fromisoformat(payload["project"]["last_activity_at"])
        assert last == when
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id = $1", proj["id"])


@pytest.mark.asyncio
async def test_last_activity_at_picks_max_across_all_sources(db_pool):
    """With state, lesson, journal, and session each having different timestamps,
    last_activity_at is the latest of them."""
    base = datetime(2050, 1, 1, 0, 0, 0)
    proj_when = base
    state_when = base + timedelta(days=10)
    lesson_when = base + timedelta(days=20)
    journal_when = base + timedelta(days=40)  # the max
    session_when = base + timedelta(days=30)

    proj = await db_pool.fetchrow(
        """INSERT INTO projects (name, updated_at) VALUES ('v6-la-mix', $1)
           RETURNING id""",
        proj_when,
    )
    await db_pool.execute(
        """INSERT INTO project_state (project_id, current_focus, updated_at)
           VALUES ($1, 'x', $2)""",
        proj["id"], state_when,
    )
    lesson = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, project_id, learned_at)
           VALUES ('la-mix-lesson', 'c', $1, $2) RETURNING id""",
        proj["id"], lesson_when,
    )
    journal = await db_pool.fetchrow(
        """INSERT INTO journal (content, project_id, entry_date)
           VALUES ('la-mix-journal', $1, $2) RETURNING id""",
        proj["id"], journal_when,
    )
    machine = await db_pool.fetchrow(
        "INSERT INTO machines (name) VALUES ('la-mix-mac') RETURNING id",
    )
    session = await db_pool.fetchrow(
        """INSERT INTO sessions (machine_id, project_id, ended_at)
           VALUES ($1, $2, $3) RETURNING id""",
        machine["id"], proj["id"], session_when,
    )
    try:
        result = await get_project(name="v6-la-mix", ctx=_ctx(db_pool))
        payload = json.loads(result)
        last = datetime.fromisoformat(payload["project"]["last_activity_at"])
        assert last == journal_when
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session["id"])
        await db_pool.execute("DELETE FROM machines WHERE id = $1", machine["id"])
        await db_pool.execute("DELETE FROM journal WHERE id = $1", journal["id"])
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson["id"])
        await db_pool.execute("DELETE FROM project_state WHERE project_id = $1", proj["id"])
        await db_pool.execute("DELETE FROM projects WHERE id = $1", proj["id"])
