"""Tests for stamp(), assert_can_write(), require_admin()."""

import pytest

from src.identity import (
    Identity, set_identity, reset_identity,
    stamp, assert_can_write, require_admin,
)


@pytest.fixture(autouse=True)
def _reset_between():
    reset_identity()
    yield
    reset_identity()


def test_stamp_returns_current_identity():
    set_identity(Identity(
        family="codex", client_id="apikey:42",
        scopes=["read", "write"], source="apikey",
    ))
    family, client_id = stamp()
    assert family == "codex"
    assert client_id == "apikey:42"


def test_stamp_defaults_when_unauth():
    family, client_id = stamp()
    assert family == "claude"
    assert client_id is None


@pytest.mark.asyncio
async def test_assert_can_write_allows_own_row(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b own', 'c', 'codex') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        await assert_can_write(db_pool, "lessons", row["id"])
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_assert_can_write_blocks_foreign_row(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b foreign', 'c', 'claude') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        with pytest.raises(PermissionError) as exc:
            await assert_can_write(db_pool, "lessons", row["id"])
        assert "codex" in str(exc.value)
        assert "claude" in str(exc.value)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_assert_can_write_shared_metadata_always_allows(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO projects (name, source_agent)
           VALUES ('rule-b-shared', 'claude') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        await assert_can_write(db_pool, "projects", row["id"])
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_assert_can_write_admin_bypass(db_pool):
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b admin', 'c', 'claude') RETURNING id""",
    )
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write", "admin"], source="apikey",
        ))
        await assert_can_write(db_pool, "lessons", row["id"])
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", row["id"])


def test_require_admin_blocks_non_admin():
    set_identity(Identity(
        family="codex", client_id="apikey:99",
        scopes=["read", "write"], source="apikey",
    ))
    with pytest.raises(PermissionError):
        require_admin()


def test_require_admin_passes_when_admin():
    set_identity(Identity(
        family="codex", client_id="apikey:99",
        scopes=["read", "write", "admin"], source="apikey",
    ))
    require_admin()


from unittest.mock import MagicMock
import json as _json

from src.server import AppContext


def _ctx(db_pool, mock_openai=None, mock_anthropic=None):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool,
        openai=mock_openai or MagicMock(),
        anthropic=mock_anthropic or MagicMock(),
    )
    return ctx


def _codex():
    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))


@pytest.mark.asyncio
async def test_log_lesson_stamps_codex(db_pool, mock_openai, mock_anthropic):
    from src.tools.lessons import log_lesson
    _codex()
    await db_pool.execute("DELETE FROM lessons WHERE title = $1", "v6-stamp-lesson")
    result = await log_lesson(
        title="v6-stamp-lesson",
        content="stamped by codex",
        ctx=_ctx(db_pool, mock_openai, mock_anthropic),
    )
    payload = _json.loads(result)
    lesson_id = payload["lesson_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent, source_client_id FROM lessons WHERE id = $1",
            lesson_id,
        )
        assert row["source_agent"] == "codex"
        assert row["source_client_id"] == "apikey:7"
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_log_pattern_stamps_codex(db_pool, mock_openai):
    from src.tools.lessons import log_pattern
    _codex()
    await db_pool.execute("DELETE FROM patterns WHERE name = $1", "v6-stamp-pattern")
    result = await log_pattern(
        name="v6-stamp-pattern",
        problem="p", solution="s",
        ctx=_ctx(db_pool, mock_openai),
    )
    payload = _json.loads(result)
    pat_id = payload["pattern_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM patterns WHERE id = $1", pat_id,
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM patterns WHERE id = $1", pat_id)


@pytest.mark.asyncio
async def test_write_journal_stamps_codex(db_pool, mock_openai):
    from src.tools.journal import write_journal
    _codex()
    result = await write_journal(
        content="codex first journal", tags=["v6"],
        ctx=_ctx(db_pool, mock_openai),
    )
    payload = _json.loads(result)
    eid = payload["entry_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent, source_client_id FROM journal WHERE id = $1", eid,
        )
        assert row["source_agent"] == "codex"
        assert row["source_client_id"] == "apikey:7"
    finally:
        await db_pool.execute("DELETE FROM journal WHERE id = $1", eid)


@pytest.mark.asyncio
async def test_rate_lesson_cross_agent_allowed(db_pool):
    """codex can rate a claude lesson (votes aren't ownership)."""
    from src.tools.lessons import rate_lesson
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-rate-x-agent', 'c', 'claude') RETURNING id""",
    )
    lesson_id = row["id"]
    _codex()
    try:
        result = await rate_lesson(
            lesson_id=lesson_id, rating="up", ctx=_ctx(db_pool),
        )
        payload = _json.loads(result)
        assert payload.get("success") is True
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_create_spec_stamps_codex(db_pool, mock_openai):
    from src.tools.specs import create_spec
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('v6-spec-proj') RETURNING id",
    )
    _codex()
    try:
        result = await create_spec(
            title="v6-stamp-spec",
            content="C", summary="S",
            project="v6-spec-proj",
            ctx=_ctx(db_pool, mock_openai),
        )
        payload = _json.loads(result)
        spec_id = payload["spec_id"]
        try:
            row = await db_pool.fetchrow(
                "SELECT source_agent FROM specifications WHERE id = $1", spec_id,
            )
            assert row["source_agent"] == "codex"
        finally:
            await db_pool.execute("DELETE FROM specifications WHERE id = $1", spec_id)
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id = $1", proj["id"])


@pytest.mark.asyncio
async def test_register_agent_stamps_codex(db_pool, mock_openai):
    from src.tools.agents import register_agent
    _codex()
    result = await register_agent(
        name="v6-stamp-agent",
        description="D",
        spec_content="C",
        summary="S",
        ctx=_ctx(db_pool, mock_openai),
    )
    payload = _json.loads(result)
    aid = payload["agent_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM agent_specs WHERE id = $1", aid,
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM agent_specs WHERE id = $1", aid)


@pytest.mark.asyncio
async def test_register_mcp_server_stamps_codex(db_pool, mock_openai):
    from src.tools.mcp_registry import register_mcp_server
    _codex()
    result = await register_mcp_server(
        name="v6-stamp-mcp",
        description="D",
        transport="stdio",
        ctx=_ctx(db_pool, mock_openai),
    )
    payload = _json.loads(result)
    sid = payload["server_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM mcp_servers WHERE id = $1", sid,
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM mcp_servers WHERE id = $1", sid)


@pytest.mark.asyncio
async def test_annotate_stamps_codex(db_pool):
    from src.tools.annotations import annotate
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-anno-target', 'c', 'claude') RETURNING id""",
    )
    lesson_id = row["id"]
    _codex()
    try:
        result = await annotate(
            entity_type="lesson",
            entity_id=lesson_id,
            note="codex says watch out",
            ctx=_ctx(db_pool),
        )
        payload = _json.loads(result)
        anno_id = payload["annotation_id"]
        try:
            arow = await db_pool.fetchrow(
                "SELECT source_agent FROM annotations WHERE id = $1", anno_id,
            )
            assert arow["source_agent"] == "codex"
        finally:
            await db_pool.execute("DELETE FROM annotations WHERE id = $1", anno_id)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_start_session_stamps_codex(db_pool):
    from src.tools.sessions import start_session
    m = await db_pool.fetchrow(
        "INSERT INTO machines (name) VALUES ('v6-sess-mac') RETURNING id",
    )
    _codex()
    try:
        result = await start_session(
            machine="v6-sess-mac", ctx=_ctx(db_pool),
        )
        payload = _json.loads(result)
        sid = payload["session_id"]
        try:
            row = await db_pool.fetchrow(
                "SELECT source_agent FROM sessions WHERE id = $1", sid,
            )
            assert row["source_agent"] == "codex"
        finally:
            await db_pool.execute("DELETE FROM sessions WHERE id = $1", sid)
    finally:
        await db_pool.execute("DELETE FROM machines WHERE id = $1", m["id"])


@pytest.mark.asyncio
async def test_add_project_stamps_codex(db_pool):
    from src.tools.admin import add_project
    await db_pool.execute("DELETE FROM projects WHERE name = $1", "v6-add-proj")
    _codex()
    result = await add_project(
        name="v6-add-proj",
        path="/tmp",
        ctx=_ctx(db_pool),
    )
    payload = _json.loads(result)
    pid = payload["project_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM projects WHERE id = $1", pid,
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id = $1", pid)


@pytest.mark.asyncio
async def test_add_machine_stamps_codex(db_pool):
    from src.tools.infra import add_machine
    await db_pool.execute("DELETE FROM machines WHERE name = $1", "v6-add-mac")
    _codex()
    result = await add_machine(
        name="v6-add-mac",
        ip="10.0.0.1",
        ctx=_ctx(db_pool),
    )
    payload = _json.loads(result)
    mid = payload["machine_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM machines WHERE id = $1", mid,
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM machines WHERE id = $1", mid)


@pytest.mark.asyncio
async def test_add_container_stamps_codex(db_pool):
    from src.tools.infra import add_container
    m = await db_pool.fetchrow(
        "INSERT INTO machines (name) VALUES ('v6-cont-mac') RETURNING id",
    )
    _codex()
    try:
        result = await add_container(
            name="v6-cont-name",
            machine="v6-cont-mac",
            project="v6-cont-proj",
            ctx=_ctx(db_pool),
        )
        payload = _json.loads(result)
        cid = payload["container_id"]
        try:
            row = await db_pool.fetchrow(
                "SELECT source_agent FROM containers WHERE id = $1", cid,
            )
            assert row["source_agent"] == "codex"
        finally:
            await db_pool.execute("DELETE FROM containers WHERE id = $1", cid)
    finally:
        await db_pool.execute("DELETE FROM machines WHERE id = $1", m["id"])


@pytest.mark.asyncio
async def test_update_project_state_stamps_writer(db_pool):
    from src.tools.admin import update_project_state
    proj = await db_pool.fetchrow(
        "INSERT INTO projects (name, source_agent) VALUES ('v6-pstate', 'claude') RETURNING id",
    )
    _codex()
    try:
        result = await update_project_state(
            project="v6-pstate",
            current_focus="codex took over",
            ctx=_ctx(db_pool),
        )
        assert "error" not in _json.loads(result)
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM project_state WHERE project_id = $1", proj["id"],
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM project_state WHERE project_id = $1", proj["id"])
        await db_pool.execute("DELETE FROM projects WHERE id = $1", proj["id"])


@pytest.mark.asyncio
async def test_codex_cannot_retire_claude_lesson(db_pool):
    from src.tools.lessons import retire_lesson
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-retire-foreign', 'c', 'claude') RETURNING id""",
    )
    lesson_id = row["id"]
    _codex()
    try:
        with pytest.raises(PermissionError) as exc:
            await retire_lesson(
                lesson_id=lesson_id, reason="codex says no", ctx=_ctx(db_pool),
            )
        assert "codex" in str(exc.value)
        assert "claude" in str(exc.value)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_codex_can_retire_own_lesson(db_pool):
    from src.tools.lessons import retire_lesson
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-retire-own', 'c', 'codex') RETURNING id""",
    )
    lesson_id = row["id"]
    _codex()
    try:
        result = await retire_lesson(
            lesson_id=lesson_id, reason="cleanup", ctx=_ctx(db_pool),
        )
        payload = _json.loads(result)
        assert payload.get("success") is True
        row = await db_pool.fetchrow(
            "SELECT retired_at, retired_by_agent FROM lessons WHERE id = $1", lesson_id,
        )
        assert row["retired_at"] is not None
        assert row["retired_by_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_codex_cannot_clear_claude_annotation(db_pool):
    from src.tools.annotations import clear_annotation
    L = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-clear-anno-target', 'c', 'claude') RETURNING id""",
    )
    A = await db_pool.fetchrow(
        """INSERT INTO annotations (entity_type, entity_id, note, source_agent)
           VALUES ('lesson', $1, 'claude wrote this', 'claude') RETURNING id""",
        L["id"],
    )
    _codex()
    try:
        with pytest.raises(PermissionError):
            await clear_annotation(
                entity_type="lesson", entity_id=L["id"], ctx=_ctx(db_pool),
            )
    finally:
        await db_pool.execute("DELETE FROM annotations WHERE id = $1", A["id"])
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", L["id"])


@pytest.mark.asyncio
async def test_merge_projects_requires_admin(db_pool):
    from src.tools.admin import merge_projects
    a = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('v6-merge-A') RETURNING id",
    )
    b = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('v6-merge-B') RETURNING id",
    )
    set_identity(Identity(
        family="claude", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    try:
        with pytest.raises(PermissionError) as exc:
            await merge_projects(
                keep="v6-merge-A", merge="v6-merge-B", ctx=_ctx(db_pool),
            )
        assert "admin" in str(exc.value).lower()
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id IN ($1, $2)", a["id"], b["id"])


@pytest.mark.asyncio
async def test_resolve_conflict_requires_admin(db_pool):
    from src.tools.consolidation import resolve_conflict
    set_identity(Identity(
        family="claude", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    with pytest.raises(PermissionError):
        await resolve_conflict(
            conflict_id=99999, resolution="kept_both", ctx=_ctx(db_pool),
        )


@pytest.mark.asyncio
async def test_search_lessons_filters_by_source_agent(db_pool, mock_openai):
    """Optional source_agent filter narrows results to a single agent family."""
    from src.tools.search import search_lessons

    L_claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-filter-claude', 'unique-search-needle-9999', 'claude') RETURNING id""",
    )
    L_codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-filter-codex', 'unique-search-needle-9999', 'codex') RETURNING id""",
    )
    try:
        result = await search_lessons(
            query=None,
            source_agent="codex",
            ctx=_ctx(db_pool, mock_openai),
        )
        payload = _json.loads(result)
        ids = {r["id"] for r in payload.get("lessons", [])}
        assert L_codex["id"] in ids
        assert L_claude["id"] not in ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", L_claude["id"], L_codex["id"],
        )


@pytest.mark.asyncio
async def test_search_lessons_default_returns_all_agents(db_pool, mock_openai):
    """Default (no source_agent kwarg) returns the shared cross-agent corpus."""
    from src.tools.search import search_lessons

    L_claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-default-claude', 'unique-default-needle-8888', 'claude') RETURNING id""",
    )
    L_codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-default-codex', 'unique-default-needle-8888', 'codex') RETURNING id""",
    )
    try:
        result = await search_lessons(
            query=None,
            ctx=_ctx(db_pool, mock_openai),
            limit=50,
        )
        payload = _json.loads(result)
        ids = {r["id"] for r in payload.get("lessons", [])}
        assert L_codex["id"] in ids
        assert L_claude["id"] in ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", L_claude["id"], L_codex["id"],
        )


@pytest.mark.asyncio
async def test_read_journal_filters_by_source_agent(db_pool, mock_openai):
    """Journal read filter narrows to a single agent family."""
    from src.tools.journal import read_journal

    J_claude = await db_pool.fetchrow(
        """INSERT INTO journal (content, tags, source_agent)
           VALUES ('claude journal entry v6', ARRAY['v6-jrn-filter']::TEXT[], 'claude') RETURNING id""",
    )
    J_codex = await db_pool.fetchrow(
        """INSERT INTO journal (content, tags, source_agent)
           VALUES ('codex journal entry v6', ARRAY['v6-jrn-filter']::TEXT[], 'codex') RETURNING id""",
    )
    try:
        result = await read_journal(
            tags=["v6-jrn-filter"],
            source_agent="codex",
            ctx=_ctx(db_pool, mock_openai),
        )
        payload = _json.loads(result)
        ids = {r["id"] for r in payload.get("entries", [])}
        assert J_codex["id"] in ids
        assert J_claude["id"] not in ids
    finally:
        await db_pool.execute(
            "DELETE FROM journal WHERE id IN ($1, $2)", J_claude["id"], J_codex["id"],
        )


@pytest.mark.asyncio
async def test_search_filters_by_source_agent(db_pool, mock_openai):
    """Unified search post-filters by source_agent when provided."""
    from src.tools.search import search

    needle = "unique-unified-search-needle-7777"
    L_claude = await db_pool.fetchrow(
        f"""INSERT INTO lessons (title, content, source_agent, embedding)
           VALUES ('v6-uni-claude', '{needle}', 'claude',
                   ('[' || array_to_string(ARRAY(SELECT 0.1 FROM generate_series(1,1536)), ',') || ']')::vector)
           RETURNING id""",
    )
    L_codex = await db_pool.fetchrow(
        f"""INSERT INTO lessons (title, content, source_agent, embedding)
           VALUES ('v6-uni-codex', '{needle}', 'codex',
                   ('[' || array_to_string(ARRAY(SELECT 0.1 FROM generate_series(1,1536)), ',') || ']')::vector)
           RETURNING id""",
    )
    try:
        result = await search(
            query=needle,
            limit=50,
            source_agent="codex",
            ctx=_ctx(db_pool, mock_openai),
        )
        payload = _json.loads(result)
        ids_and_types = {(r["type"], r["id"]) for r in payload.get("results", [])}
        assert ("lesson", L_codex["id"]) in ids_and_types
        assert ("lesson", L_claude["id"]) not in ids_and_types
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", L_claude["id"], L_codex["id"],
        )


@pytest.mark.asyncio
async def test_end_session_stamps_project_state(db_pool, mock_openai):
    from src.tools.sessions import start_session, end_session
    m = await db_pool.fetchrow(
        "INSERT INTO machines (name) VALUES ('v6-end-mac') RETURNING id",
    )
    p = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('v6-end-proj') RETURNING id",
    )
    _codex()
    try:
        s = _json.loads(await start_session(
            machine="v6-end-mac", project="v6-end-proj",
            ctx=_ctx(db_pool, mock_openai),
        ))
        sid = s["session_id"]
        await end_session(
            session_id=sid, summary="codex did stuff",
            ctx=_ctx(db_pool, mock_openai),
        )
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM project_state WHERE project_id = $1", p["id"],
        )
        assert row["source_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM project_state WHERE project_id = $1", p["id"])
        await db_pool.execute("DELETE FROM sessions WHERE project_id = $1", p["id"])
        await db_pool.execute("DELETE FROM projects WHERE id = $1", p["id"])
        await db_pool.execute("DELETE FROM machines WHERE id = $1", m["id"])


@pytest.mark.asyncio
async def test_list_clients_requires_admin(db_pool):
    from src.tools.admin import list_clients
    set_identity(Identity(
        family="claude", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    with pytest.raises(PermissionError):
        await list_clients(ctx=_ctx(db_pool))


@pytest.mark.asyncio
async def test_list_clients_returns_api_keys_and_oauth(db_pool):
    from src.tools.admin import list_clients
    import hashlib

    raw = "list-clients-bearer"
    h = hashlib.sha256(raw.encode()).hexdigest()
    key = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, label)
           VALUES ($1, 'codex', 'list-test') RETURNING id""",
        h,
    )
    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ('client_list_test', 'sec', 'claude-code-test', 'none',
                   extract(epoch from NOW())::bigint, '{}'::jsonb)
           ON CONFLICT (client_id) DO NOTHING""",
    )

    set_identity(Identity(
        family="claude", client_id="apikey:99",
        scopes=["read", "write", "admin"], source="apikey",
    ))
    try:
        result = await list_clients(ctx=_ctx(db_pool))
        payload = _json.loads(result)
        sources = {r["source"] for r in payload["clients"]}
        assert "api_key" in sources
        assert "oauth" in sources
        assert any(
            r["source"] == "api_key" and r["label"] == "list-test"
            for r in payload["clients"]
        )
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key["id"])
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = 'client_list_test'")
