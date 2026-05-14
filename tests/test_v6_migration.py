"""Verify v6 migration produces expected schema state."""

import pytest


@pytest.mark.asyncio
async def test_api_keys_table_exists(db_pool):
    cols = await db_pool.fetch("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'api_keys'
    """)
    names = {r["column_name"] for r in cols}
    assert names >= {
        "id", "api_key_hash", "family", "client_name", "label",
        "scopes", "created_at", "last_seen_at", "revoked_at",
    }


@pytest.mark.asyncio
async def test_oauth_client_family_table_exists(db_pool):
    cols = await db_pool.fetch("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'oauth_client_family'
    """)
    names = {r["column_name"] for r in cols}
    assert names >= {"client_id", "family", "client_name", "inferred_from", "inferred_at"}


@pytest.mark.asyncio
async def test_source_agent_on_owned_tables(db_pool):
    owned = ["lessons", "patterns", "journal", "agent_specs", "specifications",
             "mcp_servers", "mcp_server_tools", "annotations"]
    for t in owned:
        cols = await db_pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            t,
        )
        names = {r["column_name"] for r in cols}
        assert "source_agent" in names, f"{t} missing source_agent"
        assert "source_client_id" in names, f"{t} missing source_client_id"


@pytest.mark.asyncio
async def test_source_agent_on_shared_tables(db_pool):
    shared = ["projects", "project_state", "approaches", "key_files", "guardrails",
              "permissions", "project_aliases", "machines", "databases", "containers",
              "sessions"]
    for t in shared:
        cols = await db_pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            t,
        )
        names = {r["column_name"] for r in cols}
        assert "source_agent" in names, f"{t} missing source_agent"


@pytest.mark.asyncio
async def test_mcp_server_projects_NOT_attributed(db_pool):
    """Junction table (composite PK, no id) is intentionally NOT given source_agent."""
    cols = await db_pool.fetch("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'mcp_server_projects'
    """)
    names = {r["column_name"] for r in cols}
    assert "source_agent" not in names


@pytest.mark.asyncio
async def test_backlog_analysis_cross_agent_column(db_pool):
    cols = await db_pool.fetch("""
        SELECT column_name, is_generated FROM information_schema.columns
        WHERE table_name = 'backlog_analysis' AND column_name = 'cross_agent'
    """)
    assert len(cols) == 1
    assert cols[0]["is_generated"] == "ALWAYS"


@pytest.mark.asyncio
async def test_backlog_analysis_source_agents_backfilled(db_pool):
    """Existing backlog_analysis rows have left/right_source_agent populated from lessons."""
    count_total = await db_pool.fetchval("SELECT COUNT(*) FROM backlog_analysis")
    if count_total == 0:
        pytest.skip("No existing backlog_analysis rows to verify backfill against")
    count_unbackfilled = await db_pool.fetchval(
        "SELECT COUNT(*) FROM backlog_analysis WHERE left_source_agent IS NULL"
    )
    # All pre-v6 rows should be backfilled from lessons.source_agent='claude'
    assert count_unbackfilled == 0


@pytest.mark.asyncio
async def test_existing_rows_backfilled(db_pool):
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lessons WHERE source_agent IS NULL OR source_agent <> 'claude'"
    )
    assert count == 0
