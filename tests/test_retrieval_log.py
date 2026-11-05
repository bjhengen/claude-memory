"""Reads that return stored memory must record what they returned, per client.

The log is how usage gets measured across clients that have no local
transcripts. It holds ids only, and a logging failure must never break a read.
"""

import asyncio
import json
import logging
import uuid
from unittest.mock import MagicMock

import pytest

import src.retrieval_log
from src.db import format_embedding
from src.identity import Identity, reset_identity, set_identity
from src.server import AppContext
from src.tools.admin import check_guardrails
from src.tools.annotations import get_annotations
from src.tools.infra import get_connectivity
from src.tools.journal import read_journal
from src.tools.projects import get_project, get_project_claude_md
from src.tools.search import find_context, search, search_lessons
from src.tools.specs import get_spec, search_specs

VEC = format_embedding([0.1] * 1536)


class FailingLogPool:
    """Real pool for reads; the retrieval_log write fails or hangs."""

    def __init__(self, pool, hang=False):
        self._pool = pool
        self._hang = hang

    def __getattr__(self, name):
        return getattr(self._pool, name)

    async def execute(self, sql, *args, **kwargs):
        if "retrieval_log" in sql:
            if self._hang:
                await asyncio.sleep(30)
            raise RuntimeError("log write failed")
        return await self._pool.execute(sql, *args, **kwargs)


def _ctx(pool, openai=None):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=pool, openai=openai or MagicMock(), anthropic=MagicMock(),
    )
    return ctx


@pytest.fixture
async def client(db_pool):
    client_id = f"apikey:rl-{uuid.uuid4().hex[:12]}"
    set_identity(Identity(family="codex", client_id=client_id,
                          scopes=["read", "write"], source="apikey"))
    yield client_id
    reset_identity()
    await db_pool.execute(
        "DELETE FROM retrieval_log WHERE source_client_id = $1", client_id)


@pytest.fixture
async def project(db_pool):
    name = f"rl-{uuid.uuid4().hex[:12]}"
    row = await db_pool.fetchrow(
        "INSERT INTO projects (name, claude_md) VALUES ($1, 'notes') RETURNING id",
        name)
    yield name, row["id"]
    pid = row["id"]
    await db_pool.execute("DELETE FROM lessons WHERE project_id = $1", pid)
    await db_pool.execute("DELETE FROM journal WHERE project_id = $1", pid)
    await db_pool.execute("DELETE FROM specifications WHERE project_id = $1", pid)
    await db_pool.execute("DELETE FROM guardrails WHERE project_id = $1", pid)
    await db_pool.execute("DELETE FROM projects WHERE id = $1", pid)


async def _only_row(pool, client_id):
    rows = await pool.fetch(
        "SELECT * FROM retrieval_log WHERE source_client_id = $1", client_id)
    assert len(rows) == 1, f"expected one log row, got {len(rows)}"
    return rows[0]


def _items(row):
    return [(i["type"], i["id"]) for i in json.loads(row["items"])]


async def _seed_lesson(pool, project_id, text="retrieval log lesson"):
    row = await pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, project_id)
           VALUES ($1, $2, $3::vector, $4) RETURNING id""",
        text, text, VEC, project_id)
    return row["id"]


async def _seed_spec(pool, project_id):
    row = await pool.fetchrow(
        """INSERT INTO specifications (title, content, summary, embedding, project_id)
           VALUES ('rl spec', 'body', 'sum', $1::vector, $2) RETURNING id""",
        VEC, project_id)
    return row["id"]


@pytest.mark.asyncio
async def test_search_records_tool_client_and_returned_ids(
        db_pool, mock_openai, client, project):
    lesson_id = await _seed_lesson(db_pool, project[1])

    output = json.loads(await search(
        query="retrieval log lesson", limit=500, ctx=_ctx(db_pool, mock_openai)))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "search"
    assert row["source_agent"] == "codex"
    assert ("lesson", lesson_id) in _items(row)
    assert set(_items(row)) == {(r["type"], r["id"]) for r in output["results"]}
    assert row["result_count"] == len(output["results"])


@pytest.mark.asyncio
async def test_search_with_no_results_still_logs_a_row(db_pool, mock_openai, client):
    await search(query="anything", limit=0, ctx=_ctx(db_pool, mock_openai))

    row = await _only_row(db_pool, client)
    assert row["result_count"] == 0
    assert _items(row) == []


@pytest.mark.asyncio
async def test_query_text_is_never_stored(db_pool, mock_openai, client, project):
    await _seed_lesson(db_pool, project[1])
    marker = f"secretquery{uuid.uuid4().hex}"
    ctx = _ctx(db_pool, mock_openai)

    await search(query=marker, limit=3, ctx=ctx)
    await search_lessons(query=marker, limit=3, ctx=ctx)
    await find_context(query=marker, ctx=ctx)
    await read_journal(query=marker, limit=3, ctx=ctx)
    await search_specs(query=marker, limit=3, ctx=ctx)

    rows = await db_pool.fetch(
        "SELECT * FROM retrieval_log WHERE source_client_id = $1", client)
    assert len(rows) == 5
    for row in rows:
        assert marker not in json.dumps(dict(row), default=str)


@pytest.mark.asyncio
async def test_search_lessons_records_lesson_ids(db_pool, client, project):
    lesson_id = await _seed_lesson(db_pool, project[1])

    await search_lessons(project=project[0], ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "search_lessons"
    assert _items(row) == [("lesson", lesson_id)]
    assert row["result_count"] == 1


@pytest.mark.asyncio
async def test_read_journal_records_entry_ids(db_pool, client, project):
    entry = await db_pool.fetchrow(
        "INSERT INTO journal (content, project_id) VALUES ('j', $1) RETURNING id",
        project[1])

    await read_journal(project=project[0], ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "read_journal"
    assert _items(row) == [("journal", entry["id"])]


@pytest.mark.asyncio
async def test_get_project_records_project_id(db_pool, client, project):
    await get_project(name=project[0], ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "get_project"
    assert _items(row) == [("project", project[1])]


@pytest.mark.asyncio
async def test_get_project_not_found_logs_zero_results(db_pool, client):
    await get_project(name=f"missing-{uuid.uuid4().hex}", ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "get_project"
    assert row["result_count"] == 0


@pytest.mark.asyncio
async def test_get_project_claude_md_records_project_id(db_pool, client, project):
    await get_project_claude_md(project=project[0], ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "get_project_claude_md"
    assert _items(row) == [("project", project[1])]


@pytest.mark.asyncio
async def test_get_project_claude_md_not_found_logs_zero_results(db_pool, client):
    await get_project_claude_md(project=f"missing-{uuid.uuid4().hex}", ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["result_count"] == 0


@pytest.mark.asyncio
async def test_find_context_records_every_tier(db_pool, mock_openai, client, project):
    lesson_id = await _seed_lesson(db_pool, project[1])
    spec_id = await _seed_spec(db_pool, project[1])
    suffix = uuid.uuid4().hex[:10]
    agent = await db_pool.fetchrow(
        """INSERT INTO agent_specs (name, description, spec_content, embedding, project_id)
           VALUES ($1, 'd', 'c', $2::vector, $3) RETURNING id""",
        f"rl-agent-{suffix}", VEC, project[1])
    server = await db_pool.fetchrow(
        """INSERT INTO mcp_servers (name, description, transport)
           VALUES ($1, 'd', 'http') RETURNING id""", f"rl-server-{suffix}")
    tool = await db_pool.fetchrow(
        """INSERT INTO mcp_server_tools (server_id, tool_name, description, embedding)
           VALUES ($1, 'rl_tool', 'd', $2::vector) RETURNING id""", server["id"], VEC)
    await db_pool.execute(
        "INSERT INTO mcp_server_projects (server_id, project_id) VALUES ($1, $2)",
        server["id"], project[1])
    try:
        await find_context(query="anything", project=project[0],
                           ctx=_ctx(db_pool, mock_openai))

        row = await _only_row(db_pool, client)
        assert row["tool"] == "find_context"
        assert set(_items(row)) == {
            ("lesson", lesson_id), ("spec", spec_id),
            ("agent", agent["id"]), ("mcp_tool", tool["id"]),
        }
        assert row["result_count"] == 4
    finally:
        await db_pool.execute("DELETE FROM mcp_server_projects WHERE server_id = $1", server["id"])
        await db_pool.execute("DELETE FROM mcp_server_tools WHERE id = $1", tool["id"])
        await db_pool.execute("DELETE FROM mcp_servers WHERE id = $1", server["id"])
        await db_pool.execute("DELETE FROM agent_specs WHERE id = $1", agent["id"])


@pytest.mark.asyncio
async def test_search_specs_records_spec_ids(db_pool, mock_openai, client, project):
    spec_id = await _seed_spec(db_pool, project[1])

    await search_specs(query="x", project=project[0], ctx=_ctx(db_pool, mock_openai))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "search_specs"
    assert _items(row) == [("spec", spec_id)]


@pytest.mark.asyncio
async def test_get_spec_records_spec_id(db_pool, client, project):
    spec_id = await _seed_spec(db_pool, project[1])

    await get_spec(spec_id=spec_id, ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "get_spec"
    assert _items(row) == [("spec", spec_id)]


@pytest.mark.asyncio
async def test_get_spec_not_found_logs_zero_results(db_pool, client):
    await get_spec(spec_id=2_000_000_000, ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["result_count"] == 0


@pytest.mark.asyncio
async def test_check_guardrails_records_guardrail_ids(db_pool, client, project):
    guardrail = await db_pool.fetchrow(
        """INSERT INTO guardrails (description, project_id, check_type)
           VALUES ('rl guard', $1, 'rl-action') RETURNING id""", project[1])

    await check_guardrails(project=project[0], action="rl-action", ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["tool"] == "check_guardrails"
    assert ("guardrail", guardrail["id"]) in _items(row)


@pytest.mark.asyncio
async def test_check_guardrails_with_none_applying_logs_a_row(db_pool, client, project):
    await db_pool.execute(
        "DELETE FROM guardrails WHERE project_id = $1", project[1])
    global_rows = await db_pool.fetchval(
        """SELECT count(*) FROM guardrails WHERE project_id IS NULL
           AND check_type IN ('always', 'rl-none')""")
    await check_guardrails(project=project[0], action="rl-none", ctx=_ctx(db_pool))

    row = await _only_row(db_pool, client)
    assert row["result_count"] == global_rows


@pytest.mark.asyncio
async def test_get_annotations_records_annotation_ids(db_pool, client, project):
    lesson_id = await _seed_lesson(db_pool, project[1])
    note = await db_pool.fetchrow(
        """INSERT INTO annotations (entity_type, entity_id, note)
           VALUES ('lesson', $1, 'n') RETURNING id""", lesson_id)
    try:
        await get_annotations(entity_type="lesson", entity_id=lesson_id, ctx=_ctx(db_pool))

        row = await _only_row(db_pool, client)
        assert row["tool"] == "get_annotations"
        assert _items(row) == [("annotation", note["id"])]
    finally:
        await db_pool.execute("DELETE FROM annotations WHERE id = $1", note["id"])


@pytest.mark.asyncio
async def test_get_connectivity_records_container_and_database_ids(db_pool, client):
    suffix = uuid.uuid4().hex[:10]
    machine = await db_pool.fetchrow(
        "INSERT INTO machines (name) VALUES ($1) RETURNING id", f"rl-m-{suffix}")
    label = f"rl-conn-{suffix}"
    container = await db_pool.fetchrow(
        """INSERT INTO containers (name, machine_id, project)
           VALUES ('c', $1, $2) RETURNING id""", machine["id"], label)
    database = await db_pool.fetchrow(
        """INSERT INTO databases (name, db_type, machine_id, project)
           VALUES ('d', 'pg', $1, $2) RETURNING id""", machine["id"], label)
    try:
        await get_connectivity(project=label, ctx=_ctx(db_pool))

        row = await _only_row(db_pool, client)
        assert row["tool"] == "get_connectivity"
        assert set(_items(row)) == {
            ("container", container["id"]), ("database", database["id"]),
        }
    finally:
        await db_pool.execute("DELETE FROM containers WHERE id = $1", container["id"])
        await db_pool.execute("DELETE FROM databases WHERE id = $1", database["id"])
        await db_pool.execute("DELETE FROM machines WHERE id = $1", machine["id"])


@pytest.mark.asyncio
async def test_a_failed_log_write_does_not_break_the_read(
        db_pool, client, project, caplog):
    lesson_id = await _seed_lesson(db_pool, project[1])

    with caplog.at_level(logging.WARNING, logger="src.retrieval_log"):
        output = json.loads(await search_lessons(
            project=project[0], ctx=_ctx(FailingLogPool(db_pool))))

    assert [l["id"] for l in output["lessons"]] == [lesson_id]
    assert "search_lessons" in caplog.text


@pytest.mark.asyncio
async def test_a_hung_log_write_does_not_hang_the_read(
        db_pool, client, project, monkeypatch):
    lesson_id = await _seed_lesson(db_pool, project[1])
    monkeypatch.setattr(src.retrieval_log, "LOG_TIMEOUT_S", 0.01)

    output = json.loads(await asyncio.wait_for(
        search_lessons(project=project[0], ctx=_ctx(FailingLogPool(db_pool, hang=True))),
        timeout=5,
    ))

    assert [l["id"] for l in output["lessons"]] == [lesson_id]
