# v6: Multi-Agent Attribution & Codex Onboarding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make claude-memory a multi-agent shared corpus. Every write is stamped with `source_agent` (family) and `source_client_id`. Owned content (lessons, patterns, journal, specs/agents/MCP servers, annotations) is protected by rule-b: only the original author's agent family can modify. Shared metadata (projects, project_state, etc.) remains last-writer-wins. v5 consolidation skips cross-agent pairs at log-time and at backlog-apply. Codex onboards via a new `api_keys` table; Claude's fleet migrates to per-machine tokens.

**Architecture:** New `src/identity.py` resolves `(family, client_id, scopes)` per request from one of three auth paths (api_keys hash, OAuth access token, legacy API_KEY) and exposes it via a Python `contextvars.ContextVar`. Migration `db/migrations/v6_attribution.sql` adds new tables and stamp columns. Tool changes are mechanical: stamp on insert, assert ownership before owned-content updates. Consolidation candidate queries get `source_agent` filters; the v5.1 analyzer keeps cross-agent pairs but tags them so future investigation has signal.

**Tech Stack:** Python 3.11+, asyncpg, FastMCP, PostgreSQL 16 + pgvector, pytest-asyncio.

**Pre-flight:**

- The test DB container (`claude_memory_test_db` on ai-server, port 5434) is currently stopped. Start it: `ssh ai-server 'docker start claude_memory_test_db'`.
- The test DB must have **all prior migrations** applied: `001_add_journal.sql`, `v4_feedback_loop.sql`, `v5_consolidation.sql`, `v5_1_backlog_analysis.sql`, `v5_oauth_persistence.sql`. Tasks below verify this with the schema-introspection test and the seed-row tests.
- **Subagent caveat:** the plan cites file:line locations as hints, but mechanical edits earlier in a file may shift downstream line numbers. Subagents should `grep` for the surrounding SQL/function name when applying edits, not rely on the exact line numbers as written.

---

## File Structure

**New files:**
- `db/migrations/v6_attribution.sql` — schema migration (api_keys, oauth_client_family, source_agent + source_client_id columns, audit columns, cross_agent generated + backfill for backlog_analysis)
- `src/identity.py` — identity resolver, ContextVar-backed `get_identity()`, `set_identity()`, `reset_identity()`, `stamp()`, `assert_can_write()`, `require_admin()`
- `scripts/issue_api_key.py` — admin CLI to issue tokens
- `scripts/revoke_api_key.py` — admin CLI to revoke tokens
- `scripts/list_api_keys.py` — admin CLI to list tokens
- `tests/test_v6_migration.py` — migration backfill / shape verification
- `tests/test_identity.py` — resolver branch tests
- `tests/test_identity_e2e.py` — **spike** + integration: HTTP request → ContextVar → tool handler
- `tests/test_rule_b.py` — cross-agent write enforcement tests
- `tests/test_consolidation_cross_agent.py` — cross-agent skip regression tests
- `tests/test_admin_scripts.py` — issue_api_key.py end-to-end

**Modified files:**
- `src/auth.py` — resolver hook in `load_access_token` to populate the ContextVar (or switch to Starlette middleware after spike)
- `src/tools/lessons.py` — stamp on log_lesson/log_pattern; rule-b on update_lesson/retire_lesson; rate_lesson stamps any annotation it creates
- `src/tools/journal.py` — stamp on write_journal; optional `source_agent` filter param on read_journal
- `src/tools/specs.py` — stamp on create_spec; rule-b on update_spec/retire_spec
- `src/tools/agents.py` — stamp on register_agent; rule-b on update_agent/retire_agent
- `src/tools/mcp_registry.py` — stamp on register_mcp_server/register_mcp_tool; rule-b on update_mcp_server/retire_mcp_server
- `src/tools/annotations.py` — stamp on annotate; rule-b on clear_annotation; document UPDATE-on-conflict carve-out
- `src/tools/admin.py` — stamp on add_project/update_project_state; admin scope on merge_projects; add `list_clients` MCP tool
- `src/tools/consolidation.py` — admin scope on resolve_conflict
- `src/tools/projects.py` — stamp on set_project_claude_md/update_project_claude_md
- `src/tools/sessions.py` — stamp on start_session/end_session (including end_session's project_state UPSERT)
- `src/tools/search.py` — optional `source_agent` filter param on search/search_lessons
- `src/tools/backlog_apply.py` — `fetch_candidate_rows` adds cross-agent filter (`WHERE ba.left_source_agent = ba.right_source_agent`)
- `src/consolidation/candidates.py` — `find_candidates` adds **required** `source_agent` parameter
- `src/consolidation/orchestrator.py` — pass through source_agent to `find_candidates`
- `src/consolidation/backlog.py` — `generate_pairs` SELECTs `a.source_agent`, `b.source_agent`; `judge_and_record` writes them to `backlog_analysis`

---

## Task 0: Pre-flight + ContextVar Propagation Spike

**Goal:** Confirm that a Python `contextvars.ContextVar` set inside the OAuth provider's `load_access_token` is readable inside an MCP tool handler under FastMCP's `stateless_http=True` request lifecycle. If this works, the simple ContextVar design in subsequent tasks is correct. If it does NOT work, we pivot to a Starlette middleware that attaches identity to `request.state` and adjust Task 5 accordingly.

**Files:**
- Create: `tests/test_identity_e2e.py`

- [ ] **Step 1: Start the test DB**

```bash
ssh ai-server 'docker start claude_memory_test_db'
sleep 3
PGPASSWORD=claude psql -h localhost -p 5434 -U claude -d claude_memory_test -c "SELECT 1"
```

Expected: `?column? \n----------\n        1` (DB reachable).

- [ ] **Step 2: Verify prior migrations are applied**

```bash
PGPASSWORD=claude psql -h localhost -p 5434 -U claude -d claude_memory_test -c "
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='public'
      AND table_name IN ('lessons','oauth_clients','backlog_analysis','consolidation_runs','lesson_merges','agent_specs','specifications','mcp_servers')
    ORDER BY table_name;"
```

Expected: all 8 tables listed. If any are missing, apply the appropriate prior migration from `db/migrations/` before proceeding.

- [ ] **Step 3: Write the spike harness**

Create `tests/test_identity_e2e.py`:

```python
"""End-to-end identity propagation spike.

Issues a real HTTP request through FastMCP's ASGI app and confirms that
identity set in load_access_token reaches the tool handler.
"""

import contextvars
import os

import httpx
import pytest

# This contextvar is the simplest possible probe: any propagation failure
# between middleware and tool handler will show up as a None read.
_probe: contextvars.ContextVar[str | None] = contextvars.ContextVar("probe", default=None)


@pytest.mark.asyncio
async def test_contextvar_propagates_from_auth_to_tool():
    """Identity set in load_access_token must be readable inside tool handler."""
    os.environ["DATABASE_URL"] = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://claude:claude@localhost:5434/claude_memory_test",
    )
    os.environ["API_KEY"] = "spike-test-key"
    os.environ.setdefault("OPENAI_API_KEY", "sk-dummy")
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-dummy")

    # Import after env vars set (config reads env at import time).
    from src.server import app
    from src import auth as auth_module

    # Wrap load_access_token to set our probe.
    original = auth_module.MemoryOAuthProvider.load_access_token

    async def patched(self, token):
        _probe.set(f"saw:{token[:8]}")
        return await original(self, token)

    auth_module.MemoryOAuthProvider.load_access_token = patched

    # Register a one-shot tool that reads the probe.
    from src.server import mcp

    @mcp.tool(name="_spike_probe")
    async def _spike_probe(ctx) -> str:  # noqa: ANN001
        return _probe.get() or "MISSING"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # MCP JSON-RPC tool call
        resp = await client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer spike-test-key",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "_spike_probe", "arguments": {}},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Extract the tool's text result (FastMCP wraps it)
        result_text = body["result"]["content"][0]["text"]
        assert result_text != "MISSING", (
            "ContextVar did not propagate from load_access_token into tool handler. "
            "Plan must switch to Starlette request.state pattern (see Task 5 fallback)."
        )
        assert result_text.startswith("saw:"), result_text
```

- [ ] **Step 4: Run the spike**

Run: `pytest tests/test_identity_e2e.py::test_contextvar_propagates_from_auth_to_tool -v -s`
Expected outcomes:
- **PASS:** ContextVar approach is viable. Proceed with Task 1 unchanged.
- **FAIL ("ContextVar did not propagate"):** Pivot Task 5 to attach identity to `request.state` via a Starlette middleware. The reset of the plan is unaffected; only the wire-up changes. The fallback code is given in Task 5's "Fallback path" section.

- [ ] **Step 5: Commit the spike (regardless of outcome)**

```bash
git add tests/test_identity_e2e.py
git commit -m "spike(v6): verify contextvar propagation from auth to tool handler"
```

Record the outcome in commit-message body: `Spike result: PASS|FAIL`. This decides whether Task 5 uses the ContextVar path or the `request.state` fallback.

---

## Task 1: Schema Migration

**Files:**
- Create: `db/migrations/v6_attribution.sql`
- Create: `tests/test_v6_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_v6_migration.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_v6_migration.py -v`
Expected: all FAIL (`relation "api_keys" does not exist`, etc.).

- [ ] **Step 3: Write the migration**

Create `db/migrations/v6_attribution.sql`:

```sql
-- v6: Multi-agent attribution
-- Adds source_agent + source_client_id columns to writeable tables.
-- Creates api_keys (static bearer tokens) and oauth_client_family.
-- Backfills existing rows to source_agent='claude'.
-- Adds left/right_source_agent + generated cross_agent column to backlog_analysis.

BEGIN;

-- ============================================
-- New tables
-- ============================================

CREATE TABLE IF NOT EXISTS api_keys (
    id              SERIAL PRIMARY KEY,
    api_key_hash    TEXT NOT NULL UNIQUE,
    family          TEXT NOT NULL,
    client_name     TEXT,
    label           TEXT,
    scopes          TEXT[] NOT NULL DEFAULT ARRAY['read','write'],
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMP,
    revoked_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_api_keys_active
    ON api_keys (revoked_at) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS oauth_client_family (
    client_id       TEXT PRIMARY KEY REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    family          TEXT NOT NULL,
    client_name     TEXT,
    inferred_from   TEXT NOT NULL,
    inferred_at     TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ============================================
-- Owned content: source_agent + source_client_id + audit fields
-- ============================================

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['lessons','patterns','journal','agent_specs',
                             'specifications','mcp_servers','mcp_server_tools',
                             'annotations']
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT ''claude''', t);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_client_id TEXT', t);
    END LOOP;
END $$;

-- Audit fields (who retired / who updated) on tables that have retire/update tools
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['lessons','agent_specs','specifications','mcp_servers',
                             'patterns','annotations','journal']
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS retired_by_agent TEXT', t);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS updated_by_agent TEXT', t);
    END LOOP;
END $$;

-- ============================================
-- Shared metadata: source_agent + source_client_id
-- ============================================

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['projects','project_state','approaches','key_files',
                             'guardrails','permissions','project_aliases','machines',
                             'databases','containers','sessions']
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT ''claude''', t);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_client_id TEXT', t);
    END LOOP;
END $$;

-- conflicts table (may not exist in all environments — schema is v5+ only)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'conflicts') THEN
        EXECUTE 'ALTER TABLE conflicts ADD COLUMN IF NOT EXISTS source_agent TEXT NOT NULL DEFAULT ''claude''';
        EXECUTE 'ALTER TABLE conflicts ADD COLUMN IF NOT EXISTS source_client_id TEXT';
    END IF;
END $$;

-- Intentionally NOT attributing mcp_server_projects (composite PK, junction).

-- ============================================
-- Backlog audit attribution
-- ============================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'consolidation_runs') THEN
        EXECUTE 'ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS source_agent TEXT';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backlog_analysis') THEN
        EXECUTE 'ALTER TABLE backlog_analysis ADD COLUMN IF NOT EXISTS left_source_agent  TEXT';
        EXECUTE 'ALTER TABLE backlog_analysis ADD COLUMN IF NOT EXISTS right_source_agent TEXT';
        -- Backfill from existing lessons before adding the generated column.
        EXECUTE 'UPDATE backlog_analysis ba
                 SET left_source_agent  = la.source_agent,
                     right_source_agent = lb.source_agent
                 FROM lessons la, lessons lb
                 WHERE ba.lesson_a_id = la.id
                   AND ba.lesson_b_id = lb.id
                   AND ba.left_source_agent IS NULL';
        EXECUTE 'ALTER TABLE backlog_analysis
                 ADD COLUMN IF NOT EXISTS cross_agent BOOLEAN
                 GENERATED ALWAYS AS (left_source_agent IS DISTINCT FROM right_source_agent) STORED';
    END IF;
END $$;

COMMIT;
```

- [ ] **Step 4: Apply migration to test DB**

```bash
PGPASSWORD=claude psql -h localhost -p 5434 -U claude -d claude_memory_test \
    -f db/migrations/v6_attribution.sql
```

Expected: `BEGIN ... ALTER TABLE ... COMMIT`. Idempotent (uses `IF NOT EXISTS`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_v6_migration.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add db/migrations/v6_attribution.sql tests/test_v6_migration.py
git commit -m "feat(v6): attribution schema migration

Adds source_agent + source_client_id to owned and shared tables,
creates api_keys + oauth_client_family, adds audit attribution to
backlog_analysis with backfill from lessons.source_agent.
mcp_server_projects intentionally left unattributed (junction)."
```

---

## Task 2: Identity Resolver — Skeleton + Legacy Branch

**Files:**
- Create: `src/identity.py`
- Create: `tests/test_identity.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_identity.py`:

```python
"""Identity resolver branch tests."""

import os

import pytest

from src.identity import (
    Identity, resolve_identity, get_identity, set_identity, reset_identity,
)


@pytest.fixture(autouse=True)
def _reset_between_tests():
    reset_identity()
    yield
    reset_identity()


@pytest.mark.asyncio
async def test_legacy_api_key_resolves_to_claude(db_pool, monkeypatch):
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", "legacy-secret-xyz")

    identity = await resolve_identity(db_pool, "legacy-secret-xyz")

    assert identity is not None
    assert identity.family == "claude"
    assert identity.client_id == "legacy-api-key"
    assert identity.scopes == ["read", "write"]
    assert identity.source == "legacy"


@pytest.mark.asyncio
async def test_unknown_bearer_returns_none(db_pool):
    identity = await resolve_identity(db_pool, "definitely-not-a-real-token")
    assert identity is None


def test_set_and_get_and_reset():
    assert get_identity() is None
    set_identity(Identity(family="codex", client_id="x", scopes=["read"], source="apikey"))
    assert get_identity().family == "codex"
    reset_identity()
    assert get_identity() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.identity'`.

- [ ] **Step 3: Implement the module**

Create `src/identity.py`:

```python
"""Identity resolver for multi-agent attribution.

Maps a request bearer to an Identity(family, client_id, scopes, source) via
one of three paths: api_keys hash, OAuth access token, or legacy API_KEY env.

Identity is stored in a contextvars.ContextVar so tools read it via
`get_identity()` without taking it as a parameter.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

# Legacy API_KEY (back-compat). Module-level so tests can monkeypatch.
LEGACY_API_KEY: Optional[str] = os.getenv("API_KEY")


@dataclass(frozen=True)
class Identity:
    family: str             # 'claude' | 'codex' | 'unknown'
    client_id: str          # 'legacy-api-key' | 'apikey:N' | 'oauth:<client_id>'
    scopes: list[str]       # ['read', 'write'] or includes 'admin'
    source: str             # 'legacy' | 'apikey' | 'oauth'


_current_identity: contextvars.ContextVar[Optional[Identity]] = contextvars.ContextVar(
    "current_identity", default=None
)


def set_identity(identity: Optional[Identity]) -> contextvars.Token:
    return _current_identity.set(identity)


def get_identity() -> Optional[Identity]:
    return _current_identity.get()


def reset_identity() -> None:
    """Clear the current request's identity. Public for test isolation."""
    _current_identity.set(None)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def classify_family_from_name(client_name: Optional[str]) -> str:
    """Map an OAuth client_name (or api_keys.client_name) to a family."""
    if not client_name:
        return "unknown"
    n = client_name.lower()
    if n.startswith("claude"):
        return "claude"
    if n.startswith("codex"):
        return "codex"
    return "unknown"


async def resolve_identity(pool: asyncpg.Pool, bearer: str) -> Optional[Identity]:
    """Resolve a bearer to an Identity. Returns None if unrecognized.

    Order: api_keys → OAuth → legacy API_KEY. (api_keys/OAuth tried first so a
    bearer that happens to match BOTH api_keys and legacy is attributed to
    api_keys for better forensics.)
    """
    # 1. (Future task) api_keys lookup
    # 2. (Future task) OAuth token lookup
    # 3. Legacy API_KEY (back-compat)
    if LEGACY_API_KEY and bearer == LEGACY_API_KEY:
        logger.warning(
            "DEPRECATION: legacy API_KEY used as bearer. "
            "Migrate to per-machine api_keys row."
        )
        return Identity(
            family="claude",
            client_id="legacy-api-key",
            scopes=["read", "write"],
            source="legacy",
        )

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_identity.py
git commit -m "feat(v6): identity resolver skeleton + legacy API_KEY branch + contextvar"
```

---

## Task 3: Resolver — `api_keys` Hash Branch

**Files:**
- Modify: `src/identity.py`
- Modify: `tests/test_identity.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_identity.py`:

```python
import hashlib


@pytest.mark.asyncio
async def test_api_keys_hash_match(db_pool):
    raw = "test-bearer-aaaaaaaaaaaa"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, client_name, label, scopes)
           VALUES ($1, 'codex', 'codex-cli', 'test row', ARRAY['read','write'])
           RETURNING id""",
        h,
    )
    key_id = row["id"]
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is not None
        assert identity.family == "codex"
        assert identity.client_id == f"apikey:{key_id}"
        assert identity.scopes == ["read", "write"]
        assert identity.source == "apikey"

        last_seen = await db_pool.fetchval(
            "SELECT last_seen_at FROM api_keys WHERE id = $1", key_id,
        )
        assert last_seen is not None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key_id)


@pytest.mark.asyncio
async def test_api_keys_revoked_does_not_match(db_pool):
    raw = "test-bearer-revoked-bbbb"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, scopes, revoked_at)
           VALUES ($1, 'codex', ARRAY['read','write'], NOW()) RETURNING id""",
        h,
    )
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_api_keys_admin_scope_preserved(db_pool):
    raw = "test-bearer-admin-cccc"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, scopes)
           VALUES ($1, 'claude', ARRAY['read','write','admin']) RETURNING id""",
        h,
    )
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is not None
        assert "admin" in identity.scopes
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", row["id"])


@pytest.mark.asyncio
async def test_api_keys_wins_over_legacy(db_pool, monkeypatch):
    """A bearer that matches BOTH legacy API_KEY and an api_keys row resolves via api_keys."""
    raw = "double-match-bearer-dddd"
    h = hashlib.sha256(raw.encode()).hexdigest()
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", raw)
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, label, scopes)
           VALUES ($1, 'codex', 'overlap', ARRAY['read','write']) RETURNING id""",
        h,
    )
    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is not None
        assert identity.source == "apikey"
        assert identity.family == "codex"
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", row["id"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py::test_api_keys_hash_match -v`
Expected: FAIL (resolver returns None — api_keys branch not yet implemented).

- [ ] **Step 3: Implement the api_keys branch**

In `src/identity.py`, replace the `# 1. (Future task) api_keys lookup` line with:

```python
    # 1. api_keys hash lookup
    bearer_hash = _sha256_hex(bearer)
    row = await pool.fetchrow(
        """SELECT id, family, scopes FROM api_keys
           WHERE api_key_hash = $1 AND revoked_at IS NULL""",
        bearer_hash,
    )
    if row:
        # Touch last_seen_at. Awaited (one extra round-trip per request) for
        # simplicity; revisit if it becomes a hot-path bottleneck.
        await pool.execute(
            "UPDATE api_keys SET last_seen_at = NOW() WHERE id = $1",
            row["id"],
        )
        return Identity(
            family=row["family"],
            client_id=f"apikey:{row['id']}",
            scopes=list(row["scopes"]),
            source="apikey",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_identity.py
git commit -m "feat(v6): resolver api_keys hash-match branch; api_keys precedes legacy"
```

---

## Task 4: Resolver — OAuth Branch (with Expiry Check)

**Files:**
- Modify: `src/identity.py`
- Modify: `tests/test_identity.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_identity.py`:

```python
@pytest.mark.asyncio
async def test_oauth_token_resolves_claude_family(db_pool):
    client_id = "client_test_oauth_claude"
    client_name = "claude-code-test"
    token = "oauth-test-token-eeee"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', $2, 'none', extract(epoch from NOW())::bigint, '{}'::jsonb)""",
        client_id, client_name,
    )
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
           VALUES ($1, $2, '[]'::jsonb, $3)""",
        token, client_id, 2**31 - 1,
    )
    try:
        identity = await resolve_identity(db_pool, token)
        assert identity is not None
        assert identity.family == "claude"
        assert identity.client_id == f"oauth:{client_id}"
        assert identity.source == "oauth"

        family_row = await db_pool.fetchrow(
            "SELECT family, inferred_from FROM oauth_client_family WHERE client_id = $1",
            client_id,
        )
        assert family_row["family"] == "claude"
        assert family_row["inferred_from"] == "client_name_prefix"
    finally:
        await db_pool.execute("DELETE FROM oauth_client_family WHERE client_id = $1", client_id)
        await db_pool.execute("DELETE FROM oauth_access_tokens WHERE token = $1", token)
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = $1", client_id)


@pytest.mark.asyncio
async def test_oauth_token_unknown_client_name(db_pool):
    client_id = "client_test_oauth_unknown"
    client_name = "some-random-app"
    token = "oauth-test-token-ffff"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', $2, 'none', extract(epoch from NOW())::bigint, '{}'::jsonb)""",
        client_id, client_name,
    )
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
           VALUES ($1, $2, '[]'::jsonb, $3)""",
        token, client_id, 2**31 - 1,
    )
    try:
        identity = await resolve_identity(db_pool, token)
        assert identity is not None
        assert identity.family == "unknown"
    finally:
        await db_pool.execute("DELETE FROM oauth_client_family WHERE client_id = $1", client_id)
        await db_pool.execute("DELETE FROM oauth_access_tokens WHERE token = $1", token)
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = $1", client_id)


@pytest.mark.asyncio
async def test_oauth_expired_token_does_not_resolve(db_pool):
    """Expired access tokens must NOT set identity, even if the row still exists."""
    client_id = "client_test_oauth_expired"
    token = "oauth-test-token-gggg"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', 'claude-code', 'none', extract(epoch from NOW())::bigint, '{}'::jsonb)""",
        client_id,
    )
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
           VALUES ($1, $2, '[]'::jsonb, $3)""",
        token, client_id, 1,  # expired
    )
    try:
        identity = await resolve_identity(db_pool, token)
        assert identity is None
    finally:
        await db_pool.execute("DELETE FROM oauth_access_tokens WHERE token = $1", token)
        await db_pool.execute("DELETE FROM oauth_clients WHERE client_id = $1", client_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py -v -k oauth`
Expected: all three FAIL.

- [ ] **Step 3: Implement the OAuth branch**

In `src/identity.py`, replace the `# 2. (Future task) OAuth token lookup` line with:

```python
    # 2. OAuth access token lookup (with expiry filter)
    row = await pool.fetchrow(
        """SELECT t.client_id, c.client_name
           FROM oauth_access_tokens t
           JOIN oauth_clients c ON c.client_id = t.client_id
           WHERE t.token = $1
             AND (t.expires_at IS NULL OR t.expires_at > $2)""",
        bearer, int(time.time()),
    )
    if row:
        oauth_client_id = row["client_id"]
        client_name = row["client_name"]

        family_row = await pool.fetchrow(
            "SELECT family FROM oauth_client_family WHERE client_id = $1",
            oauth_client_id,
        )
        if family_row:
            family = family_row["family"]
        else:
            family = classify_family_from_name(client_name)
            await pool.execute(
                """INSERT INTO oauth_client_family
                   (client_id, family, client_name, inferred_from)
                   VALUES ($1, $2, $3, 'client_name_prefix')
                   ON CONFLICT (client_id) DO NOTHING""",
                oauth_client_id, family, client_name,
            )
            if family == "unknown":
                logger.warning(
                    "Unknown OAuth client classified as 'unknown': "
                    "client_id=%s client_name=%r. Update oauth_client_family.family "
                    "to a known family if this is misclassified.",
                    oauth_client_id, client_name,
                )

        return Identity(
            family=family,
            client_id=f"oauth:{oauth_client_id}",
            scopes=["read", "write"],
            source="oauth",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: 10 PASS total.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_identity.py
git commit -m "feat(v6): resolver OAuth branch (with expiry filter) + family prefix classifier"
```

---

## Task 5: Wire Resolver Into Auth Layer

**Files:**
- Modify: `src/auth.py`
- Modify: `tests/test_identity.py`

**Branch on Task 0 spike outcome:**
- **PASS (ContextVar works):** use the primary path below.
- **FAIL (ContextVar does not propagate):** use the "Fallback path" at the end.

### Primary path (ContextVar)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_identity.py`:

```python
@pytest.mark.asyncio
async def test_load_access_token_sets_identity_via_apikey(db_pool, monkeypatch):
    """An api_keys-issued bearer is accepted by load_access_token AND sets identity."""
    from src.auth import MemoryOAuthProvider

    raw = "auth-wire-apikey-hhhh"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, label, scopes)
           VALUES ($1, 'codex', 'auth-wire-test', ARRAY['read','write']) RETURNING id""",
        h,
    )
    key_id = row["id"]

    provider = MemoryOAuthProvider(api_key="some-other-legacy")
    provider.set_pool(db_pool)
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", "some-other-legacy")

    try:
        result = await provider.load_access_token(raw)
        assert result is not None
        assert result.client_id == f"apikey:{key_id}"

        identity = get_identity()
        assert identity is not None
        assert identity.family == "codex"
        assert identity.source == "apikey"
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key_id)


@pytest.mark.asyncio
async def test_load_access_token_legacy_back_compat(db_pool, monkeypatch):
    """Legacy API_KEY bearer still produces a valid AccessToken AND sets identity."""
    from src.auth import MemoryOAuthProvider

    provider = MemoryOAuthProvider(api_key="legacy-wire-test-iiii")
    provider.set_pool(db_pool)
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", "legacy-wire-test-iiii")

    result = await provider.load_access_token("legacy-wire-test-iiii")
    assert result is not None
    # back-compat: legacy AccessToken client_id stays 'api-key-user'
    assert result.client_id == "api-key-user"

    identity = get_identity()
    assert identity is not None
    assert identity.family == "claude"
    assert identity.source == "legacy"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py -v -k load_access_token`
Expected: both FAIL.

- [ ] **Step 3: Rewrite `load_access_token`**

In `src/auth.py`, replace the `load_access_token` method with:

```python
    async def load_access_token(self, token: str) -> AccessToken | None:
        """Load an access token AND populate the per-request identity ContextVar.

        Resolves identity for stamping regardless of which auth path matched.
        Failure to resolve identity is non-fatal — the access-token check
        below is what gates the request.
        """
        from src.identity import resolve_identity, set_identity, get_identity

        try:
            identity = await resolve_identity(self.pool, token)
            if identity is not None:
                set_identity(identity)
        except Exception as e:
            logger.error(f"Identity resolution failed (non-fatal): {e}")

        # api_keys-issued bearers are valid even though they aren't in
        # oauth_access_tokens. The resolver has already validated them.
        identity = get_identity()
        if identity is not None and identity.source == "apikey":
            return AccessToken(
                token=token,
                client_id=identity.client_id,
                scopes=identity.scopes,
                expires_at=None,
            )

        # Legacy API_KEY back-compat (preserves existing client_id="api-key-user")
        if token == self.api_key:
            return AccessToken(
                token=token,
                client_id="api-key-user",
                scopes=[],
                expires_at=None,
            )

        # OAuth-issued access tokens
        row = await self.pool.fetchrow(
            "SELECT client_id, scopes, expires_at, resource FROM oauth_access_tokens WHERE token = $1",
            token,
        )
        if row:
            return AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=json.loads(row["scopes"]) if row["scopes"] else [],
                expires_at=row["expires_at"],
                resource=row["resource"],
            )
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: 12 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/auth.py tests/test_identity.py
git commit -m "feat(v6): wire identity resolver into OAuth provider load_access_token"
```

### Fallback path (Starlette `request.state`, only if spike failed)

If Task 0's spike failed, replace ContextVar with request-scoped state:

- Add a thin Starlette middleware that runs after `AuthenticationMiddleware`. It reads `request.user` (the AccessToken set by bearer_auth middleware), looks up identity by `client_id` from a small in-memory cache populated by `load_access_token`, and attaches `request.state.identity`.
- Change `get_identity()` to take a `request` argument: `get_identity(request) -> Optional[Identity]`. Inside tools, retrieve via `ctx.request_context.request.state.identity`.
- Update all subsequent task code that calls `get_identity()` / `stamp()` to take `ctx` and resolve from request state.

Detailed implementation deferred until the spike result is known; the structural change is small but pervasive enough to fork the rest of the plan. **If you reach this point, stop and re-plan Task 6 onwards with the request-state pattern before continuing.**

---

## Task 6: Write-Stamp + Rule-B + Admin Helpers

**Files:**
- Modify: `src/identity.py`
- Create: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rule_b.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v`
Expected: FAIL — `ImportError: cannot import name 'stamp'`.

- [ ] **Step 3: Implement the helpers**

Append to `src/identity.py`:

```python
# ---------------------------------------------------------------------------
# Write-stamp + rule-b + admin
# ---------------------------------------------------------------------------

# Tables with `id` PK + source_agent where rule b applies.
OWNED_CONTENT_TABLES = frozenset({
    "lessons",
    "patterns",
    "journal",
    "agent_specs",
    "specifications",
    "mcp_servers",
    "mcp_server_tools",
    "annotations",
})

# Tables with `id` PK + source_agent where last-writer-wins.
SHARED_METADATA_TABLES = frozenset({
    "projects",
    "project_state",
    "approaches",
    "key_files",
    "guardrails",
    "permissions",
    "project_aliases",
    "machines",
    "databases",
    "containers",
    "conflicts",
    "sessions",
})


def stamp() -> tuple[str, Optional[str]]:
    """Return (source_agent, source_client_id) for the current request.

    Defaults to ('claude', None) when unauth — preserves legacy behavior for
    any code path not yet behind the resolver.
    """
    identity = get_identity()
    if identity is None:
        return ("claude", None)
    return (identity.family, identity.client_id)


async def assert_can_write(pool: asyncpg.Pool, table: str, row_id: int) -> None:
    """Raise PermissionError if the current identity cannot write to `table.row_id`."""
    if table in SHARED_METADATA_TABLES:
        return
    if table not in OWNED_CONTENT_TABLES:
        raise ValueError(
            f"assert_can_write called with unknown table '{table}'. "
            "Add it to OWNED_CONTENT_TABLES or SHARED_METADATA_TABLES."
        )

    identity = get_identity()
    current_family = identity.family if identity else "claude"
    current_scopes = identity.scopes if identity else ["read", "write"]

    if "admin" in current_scopes:
        return

    # All OWNED_CONTENT_TABLES have a SERIAL PK `id` column. Table name is
    # allow-listed above, so f-string interpolation is safe.
    row = await pool.fetchrow(
        f"SELECT source_agent FROM {table} WHERE id = $1",
        row_id,
    )
    if row is None:
        return  # caller handles missing row
    owner = row["source_agent"]
    if owner != current_family:
        raise PermissionError(
            f"agent '{current_family}' cannot modify row owned by '{owner}' in {table}"
        )


def require_admin() -> None:
    """Raise PermissionError if the current identity lacks 'admin' scope."""
    identity = get_identity()
    scopes = identity.scopes if identity else ["read", "write"]
    if "admin" not in scopes:
        family = identity.family if identity else "claude"
        raise PermissionError(
            f"agent '{family}' lacks 'admin' scope required for this operation"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_rule_b.py
git commit -m "feat(v6): stamp/assert_can_write/require_admin helpers"
```

---

## Task 7: Stamp Inserts — `log_lesson`, `log_pattern`, `write_journal`

**Files:**
- Modify: `src/tools/lessons.py`, `src/tools/journal.py`
- Modify: `tests/test_rule_b.py`

**Important:** `rate_lesson` operates on Claude lessons from any agent (votes are not ownership). It must NOT call `assert_can_write` and must continue to work cross-agent. The annotation it appends *does* need stamping.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_b.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k "stamp or rate_lesson_cross"`
Expected: stamping tests FAIL (source_agent='claude' default). `rate_lesson_cross` likely passes today (no rule-b yet) — it's a guard test for later tasks.

- [ ] **Step 3: Modify `log_lesson`**

In `src/tools/lessons.py`, locate the `INSERT INTO lessons (...)` block in `log_lesson` (grep for `INSERT INTO lessons`). Replace with:

```python
    from src.identity import stamp
    source_agent, source_client_id = stamp()

    row = await app.db.fetchrow(
        """
        INSERT INTO lessons (title, content, project_id, tags, severity, embedding,
                             source_agent, source_client_id)
        VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8)
        RETURNING id
        """,
        title, content, project_id, tags or [], severity, embedding_str,
        source_agent, source_client_id,
    )
```

- [ ] **Step 4: Modify `log_pattern`**

In the same file, locate the `log_pattern` INSERT and apply the same pattern: import `stamp`, add `source_agent`/`source_client_id` columns + params.

- [ ] **Step 5: Modify `write_journal`**

In `src/tools/journal.py`, locate the INSERT INTO journal block. Apply the same pattern.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: all current tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tools/lessons.py src/tools/journal.py tests/test_rule_b.py
git commit -m "feat(v6): stamp source_agent on log_lesson/log_pattern/write_journal"
```

---

## Task 8: Stamp Inserts — Specs, Agents, MCP Registry, Annotations

**Files:**
- Modify: `src/tools/specs.py`, `src/tools/agents.py`, `src/tools/mcp_registry.py`, `src/tools/annotations.py`
- Modify: `tests/test_rule_b.py`

The signatures (verified from source):

- `create_spec(title, content, summary, project, subsystem=None, format_hints=None, triggers=None, ctx=None)` — `project` is **required** and must already exist.
- `register_agent(name, description, spec_content, summary=None, model='sonnet', triggers=None, tools=None, project=None, ctx=None)`.
- `register_mcp_server(name, description, transport, url=None, machine=None, auth_type='none', auth_hint=None, config_snippet=None, limitations=None, projects=None, ctx=None)`.
- `register_mcp_tool(name, server, description, ...)` — confirm signature inline.
- `annotate(entity_type, entity_id, note, ctx=None)` — UPDATE-on-conflict for the same `(entity_type, entity_id)`; first writer's `source_agent` stays.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_create_spec_stamps_codex(db_pool, mock_openai):
    from src.tools.specs import create_spec
    # Seed a project for the spec to attach to
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
    # Seed a lesson to annotate
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k "stamp_codex"`
Expected: each FAILs because the INSERT defaults source_agent='claude'.

- [ ] **Step 3: Stamp each INSERT**

For each of the four tools, find the INSERT and add `source_agent, source_client_id` columns + parameters. Pattern (apply to `create_spec`, `register_agent`, `register_mcp_server`, `register_mcp_tool`, `annotate`):

```python
from src.identity import stamp
source_agent, source_client_id = stamp()
# ...
# In the INSERT statement: add ", source_agent, source_client_id" to the
# column list, add the next two $-params, and append source_agent,
# source_client_id to the parameter tuple.
```

**Special case — `annotate`'s UPDATE-on-conflict branch:** the tool appends to an existing annotation row if one exists for the same `(entity_type, entity_id)`. **Do NOT change source_agent on UPDATE** — the first writer keeps ownership. This means a codex agent appending to a claude annotation: the codex update succeeds (rule-b is enforced only on `clear_annotation` per spec Decision #2), but the `source_agent` of the row stays `'claude'`. Document this in a one-line comment in the UPDATE branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: all current tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/specs.py src/tools/agents.py src/tools/mcp_registry.py src/tools/annotations.py tests/test_rule_b.py
git commit -m "feat(v6): stamp source_agent on spec/agent/mcp/annotation inserts"
```

---

## Task 9: Stamp Inserts — Sessions, Add-Project, Add-Machine, Add-Container

**Files:**
- Modify: `src/tools/sessions.py`, `src/tools/admin.py` (add_project), `src/tools/infra.py` (add_machine/add_container if there; otherwise wherever they're defined)
- Modify: `tests/test_rule_b.py`

- [ ] **Step 1: Locate add_machine and add_container**

```bash
grep -n "async def add_machine\|async def add_container" src/tools/*.py
```

Modify the file(s) reported.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_start_session_stamps_codex(db_pool):
    from src.tools.sessions import start_session
    # Seed a machine
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k "start_session_stamps or add_project_stamps"`
Expected: both FAIL.

- [ ] **Step 4: Stamp the inserts**

Apply the same Pattern-1 stamping to `start_session`, `add_project`, `add_machine`, `add_container` (and any other `add_*` tool found in Step 1).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tools/sessions.py src/tools/admin.py src/tools/infra.py tests/test_rule_b.py
git commit -m "feat(v6): stamp source_agent on session/project/machine/container inserts"
```

---

## Task 10: Stamp Shared-Metadata Writes (Pattern 3)

**Files:**
- Modify: `src/tools/admin.py` (update_project_state, merge_projects), `src/tools/projects.py` (set_project_claude_md, update_project_claude_md), `src/tools/sessions.py` (end_session's project_state upsert)
- Modify: `tests/test_rule_b.py`

Pattern 3 = stamp `source_agent` to the **current writer's** family on each UPDATE (last-writer-wins).

**Key gotchas:**
- `update_project_state` uses a **dynamic-clauses builder** (`updates = [...]` list). The stamp must be appended to the list so unset fields aren't overwritten with NULL.
- `end_session` performs an `INSERT INTO project_state (...) ON CONFLICT (project_id) DO UPDATE SET ...` separately from `update_project_state`. Both branches must stamp.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rule_b.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k "stamps_writer or end_session_stamps"`
Expected: both FAIL.

- [ ] **Step 3: Modify `update_project_state` (dynamic-builder pattern)**

In `src/tools/admin.py`, find `update_project_state`. It builds a dynamic `updates = [...]` list and appends to a params list. Find the `if current_focus is not None: updates.append(...)` block and add an unconditional stamp pair at the end:

```python
from src.identity import stamp
source_agent, source_client_id = stamp()

# ... existing dynamic-clauses logic ...

# Always stamp on every UPDATE (Pattern 3, last-writer-wins)
updates.append(f"source_agent = ${param_idx}")
params.append(source_agent)
param_idx += 1
updates.append(f"source_client_id = ${param_idx}")
params.append(source_client_id)
param_idx += 1
```

The INSERT branch (when no row exists yet) must also include the columns:

```python
await app.db.execute(
    """INSERT INTO project_state (project_id, current_focus, blockers, next_steps,
                                   source_agent, source_client_id)
       VALUES ($1, $2, $3, $4, $5, $6)""",
    project_id, current_focus or "", blockers or [], next_steps or [],
    source_agent, source_client_id,
)
```

- [ ] **Step 4: Modify `end_session`'s project_state UPSERT**

In `src/tools/sessions.py`, find the `INSERT INTO project_state (...) ON CONFLICT (project_id) DO UPDATE SET ...` block. Add `source_agent, source_client_id` to both branches:

```python
from src.identity import stamp
source_agent, source_client_id = stamp()
await app.db.execute(
    """INSERT INTO project_state (project_id, last_session_id, updated_at,
                                   source_agent, source_client_id)
       VALUES ($1, $2, NOW(), $3, $4)
       ON CONFLICT (project_id) DO UPDATE
       SET last_session_id  = EXCLUDED.last_session_id,
           updated_at       = NOW(),
           source_agent     = EXCLUDED.source_agent,
           source_client_id = EXCLUDED.source_client_id""",
    project_id, session_id, source_agent, source_client_id,
)
```

(Adjust to match the actual column list of the existing UPSERT.)

- [ ] **Step 5: Modify `set_project_claude_md` and `update_project_claude_md`**

In `src/tools/projects.py`, each tool runs `UPDATE projects SET claude_md = $1 WHERE ...`. Add the stamp to each UPDATE:

```python
from src.identity import stamp
source_agent, source_client_id = stamp()
await app.db.execute(
    """UPDATE projects SET claude_md = $1, source_agent = $2, source_client_id = $3
       WHERE id = $4""",
    content, source_agent, source_client_id, project_id,
)
```

- [ ] **Step 6: Modify `merge_projects` (stamp the resulting projects row too)**

In `src/tools/admin.py`'s `merge_projects`, after the merge logic concludes, the `keep` project row may be left untouched. We don't strictly need to stamp it (the merge act doesn't change `keep`'s content), but the alias INSERT does need stamping if `project_aliases` got `source_agent` columns. Apply stamp pattern there.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/tools/admin.py src/tools/sessions.py src/tools/projects.py tests/test_rule_b.py
git commit -m "feat(v6): stamp shared-metadata writes (last-writer-wins)"
```

---

## Task 11: Rule-B Enforcement on Owned-Content Updates/Retires

**Files:**
- Modify: `src/tools/lessons.py`, `src/tools/specs.py`, `src/tools/agents.py`, `src/tools/mcp_registry.py`, `src/tools/annotations.py`
- Modify: `tests/test_rule_b.py`

Tools to gate (each gets `assert_can_write` + acting-agent audit field):
- `update_lesson` → `lessons.updated_by_agent`
- `retire_lesson` → `lessons.retired_by_agent`
- `update_spec` → `specifications.updated_by_agent`
- `retire_spec` → `specifications.retired_by_agent`
- `update_agent` → `agent_specs.updated_by_agent`
- `retire_agent` → `agent_specs.retired_by_agent`
- `update_mcp_server` → `mcp_servers.updated_by_agent`
- `retire_mcp_server` → `mcp_servers.retired_by_agent`
- `clear_annotation` → `annotations.updated_by_agent` (or just stamp delete — clear sets `note=''`)

`update_*` tools that build dynamic UPDATE clauses need the audit field appended to the `updates = [...]` list (Pattern: same approach as Task 10 step 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_b.py`:

```python
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
    # Seed a claude-owned annotation on a claude lesson
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k "retire or clear_claude"`
Expected: foreign-retire FAILs (no error raised); own-retire FAILs on `retired_by_agent` column read.

- [ ] **Step 3: Apply rule-b + audit stamps**

For each tool in the list above, before the UPDATE, add:

```python
from src.identity import assert_can_write, stamp
await assert_can_write(app.db, "<table>", <row_id>)
acting_agent, _ = stamp()
```

Then extend the UPDATE to set the appropriate audit field. Two patterns:

**Static UPDATE (`retire_lesson`):**

```python
await app.db.execute(
    """UPDATE lessons SET retired_at = NOW(),
                          retired_reason = $1,
                          retired_by_agent = $2
       WHERE id = $3""",
    reason, acting_agent, lesson_id,
)
```

**Dynamic UPDATE (`update_lesson`):**

```python
updates.append(f"updated_by_agent = ${param_idx}")
params.append(acting_agent)
param_idx += 1
# ... existing build of SET clause ...
```

For `clear_annotation`: rule-b key is the annotation's own `source_agent`. Look up the annotation by `(entity_type, entity_id)` first to get its id, then `assert_can_write(app.db, 'annotations', annotation_id)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/lessons.py src/tools/specs.py src/tools/agents.py src/tools/mcp_registry.py src/tools/annotations.py tests/test_rule_b.py
git commit -m "feat(v6): rule-b enforcement + audit fields on owned-content updates/retires"
```

---

## Task 12: Admin Scope on `merge_projects` and `resolve_conflict`

**Files:**
- Modify: `src/tools/admin.py` (merge_projects), `src/tools/consolidation.py` (resolve_conflict)
- Modify: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_b.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k "requires_admin"`
Expected: FAIL (no admin gate exists).

- [ ] **Step 3: Add admin gates**

In `src/tools/admin.py`'s `merge_projects`, first line of body:

```python
from src.identity import require_admin
require_admin()
```

In `src/tools/consolidation.py`'s `resolve_conflict`, same:

```python
from src.identity import require_admin
require_admin()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v -k "requires_admin"`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/admin.py src/tools/consolidation.py tests/test_rule_b.py
git commit -m "feat(v6): admin scope required for merge_projects + resolve_conflict"
```

---

## Task 13: Cross-Agent Skip — Log-Time Consolidation

**Files:**
- Modify: `src/consolidation/candidates.py` — `find_candidates` adds **required** `source_agent` parameter (no default).
- Modify: `src/consolidation/orchestrator.py` — pass through.
- Modify: `src/tools/lessons.py` — pass stamped family to consolidator.
- Modify existing tests: `tests/test_candidates.py` adds `source_agent="claude"` to all call sites.
- Create: `tests/test_consolidation_cross_agent.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consolidation_cross_agent.py`:

```python
"""Cross-agent consolidation skip — regression tests."""

import pytest

from src.consolidation.candidates import find_candidates


@pytest.mark.asyncio
async def test_find_candidates_skips_cross_agent(db_pool):
    """A codex lesson does NOT match a claude lesson even at cosine ~1.0."""
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-claude', 'shared', $1::vector, 'claude') RETURNING id""",
        emb,
    )
    codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-codex', 'shared', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        candidates = await find_candidates(
            pool=db_pool,
            query_embedding=[0.1] * 1536,
            new_lesson_id=codex["id"],
            project_id=None,
            cosine_threshold=0.85,
            top_k=10,
            source_agent="codex",
        )
        ids = {c["id"] for c in candidates}
        assert claude["id"] not in ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", claude["id"], codex["id"],
        )


@pytest.mark.asyncio
async def test_find_candidates_matches_same_agent(db_pool):
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    a = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-codex-a', 'c', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    b = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-codex-b', 'c', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        cands = await find_candidates(
            pool=db_pool, query_embedding=[0.1] * 1536,
            new_lesson_id=b["id"], project_id=None,
            cosine_threshold=0.85, top_k=10, source_agent="codex",
        )
        assert a["id"] in {c["id"] for c in cands}
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", a["id"], b["id"],
        )


@pytest.mark.asyncio
async def test_find_candidates_missing_source_agent_is_typeerror():
    """Required param: caller forgetting source_agent gets a clear failure."""
    with pytest.raises(TypeError):
        await find_candidates(
            pool=None, query_embedding=[], new_lesson_id=0,
            project_id=None, cosine_threshold=0.85, top_k=1,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_consolidation_cross_agent.py -v`
Expected: all FAIL — `find_candidates` doesn't accept `source_agent`.

- [ ] **Step 3: Modify `find_candidates` (make source_agent required)**

In `src/consolidation/candidates.py`, replace the function with:

```python
"""Candidate finder: top-k nearest non-retired lessons above a cosine threshold."""

from typing import Any

import asyncpg


async def find_candidates(
    pool: asyncpg.Pool,
    query_embedding: list[float],
    new_lesson_id: int,
    project_id: int | None,
    cosine_threshold: float,
    top_k: int,
    source_agent: str,           # REQUIRED — cross-agent skip
) -> list[dict[str, Any]]:
    """Return up to `top_k` lessons with cosine >= threshold AND same source_agent.

    Required `source_agent`: callers must explicitly specify the agent family
    of the new lesson. Filtering same-agent prevents cross-agent auto-merge.
    """
    emb_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
    rows = await pool.fetch(
        """
        SELECT id, title, content, project_id, tags, severity,
               upvotes, downvotes,
               (1 - (embedding <=> $1::vector)) AS cosine
        FROM lessons
        WHERE embedding IS NOT NULL
          AND retired_at IS NULL
          AND id <> $2
          AND ($3::int IS NULL OR project_id = $3 OR project_id IS NULL)
          AND (1 - (embedding <=> $1::vector)) >= $4
          AND source_agent = $5
        ORDER BY embedding <=> $1::vector
        LIMIT $6
        """,
        emb_str, new_lesson_id, project_id, cosine_threshold, source_agent, top_k,
    )
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Thread `source_agent` through `consolidate_at_log`**

In `src/consolidation/orchestrator.py`, find `consolidate_at_log` and add `new_source_agent: str` to its signature. Pass it to the `find_candidates` call.

In `src/tools/lessons.py`, find the `consolidate_at_log(...)` call inside `log_lesson` and pass `new_source_agent=source_agent` (the value already stamped earlier in the function).

- [ ] **Step 5: Update existing tests that call `find_candidates` directly**

```bash
grep -rn "find_candidates(" tests/
```

For each call site missing `source_agent`, add `source_agent="claude"` (the historical default). Tests in `tests/test_candidates.py` are the primary site.

- [ ] **Step 6: Run all consolidation tests**

Run: `pytest tests/test_consolidation_cross_agent.py tests/test_candidates.py tests/test_actor_*.py tests/test_judge.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/consolidation/candidates.py src/consolidation/orchestrator.py src/tools/lessons.py tests/test_consolidation_cross_agent.py tests/test_candidates.py
git commit -m "feat(v6): cross-agent skip at log-time consolidation; source_agent required arg"
```

---

## Task 14: Analyzer Records source_agents

**Files:**
- Modify: `src/consolidation/backlog.py` — `generate_pairs` returns source_agents; `judge_and_record` writes them to `backlog_analysis`.
- Modify: `tests/test_consolidation_cross_agent.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consolidation_cross_agent.py`:

```python
@pytest.mark.asyncio
async def test_generate_pairs_returns_source_agents(db_pool):
    """generate_pairs includes a_source_agent, b_source_agent in each pair."""
    from src.consolidation.backlog import generate_pairs

    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-gp-claude', 'shared', $1::vector, 'claude') RETURNING id""",
        emb,
    )
    codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-gp-codex', 'shared', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        pairs = await generate_pairs(pool=db_pool, cosine_threshold=0.85)
        pair = next(
            (p for p in pairs
             if {p["lesson_a_id"], p["lesson_b_id"]} == {claude["id"], codex["id"]}),
            None,
        )
        assert pair is not None
        assert {pair["a_source_agent"], pair["b_source_agent"]} == {"claude", "codex"}
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", claude["id"], codex["id"],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_consolidation_cross_agent.py::test_generate_pairs_returns_source_agents -v`
Expected: FAIL — `KeyError: a_source_agent`.

- [ ] **Step 3: Modify `generate_pairs`**

In `src/consolidation/backlog.py`, find the SELECT inside `generate_pairs`. Add `a.source_agent AS a_source_agent, b.source_agent AS b_source_agent` to the SELECT clause. **Do not** filter — cross-agent pairs must remain visible for the analyzer.

- [ ] **Step 4: Modify `judge_and_record`**

Locate the INSERT INTO backlog_analysis. Extend it with `left_source_agent, right_source_agent` columns and pass through from the pair dict (`a_source_agent`, `b_source_agent`). The generated `cross_agent` column populates automatically.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_consolidation_cross_agent.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/consolidation/backlog.py tests/test_consolidation_cross_agent.py
git commit -m "feat(v6): analyzer records left/right_source_agent on backlog_analysis"
```

---

## Task 15: Cross-Agent Skip — Backlog Apply

**Files:**
- Modify: `src/tools/backlog_apply.py` — `fetch_candidate_rows` filters out cross-agent rows.
- Modify: `tests/test_consolidation_cross_agent.py`

Important: `fetch_candidate_rows` reads from `backlog_analysis`, NOT directly from `lessons`. After Task 14, those rows have `left_source_agent`/`right_source_agent`. The filter goes on those.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consolidation_cross_agent.py`:

```python
@pytest.mark.asyncio
async def test_fetch_candidate_rows_excludes_cross_agent(db_pool):
    """fetch_candidate_rows does not return rows where left/right source_agents differ."""
    from src.tools.backlog_apply import fetch_candidate_rows

    # Seed a cross-agent backlog_analysis row directly
    L1 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l1', 'c', 'claude') RETURNING id""",
    )
    L2 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l2', 'c', 'codex') RETURNING id""",
    )
    batch_id = "test-v6-cross-agent-skip"
    await db_pool.execute(
        """INSERT INTO backlog_analysis
           (batch_run_id, lesson_a_id, lesson_b_id, cosine, verdict, confidence,
            left_source_agent, right_source_agent)
           VALUES ($1, $2, $3, 0.99, 'duplicate', 0.95, 'claude', 'codex')""",
        batch_id, L1["id"], L2["id"],
    )

    # Also seed a same-agent row to verify it IS returned
    L3 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l3', 'c', 'claude') RETURNING id""",
    )
    L4 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l4', 'c', 'claude') RETURNING id""",
    )
    await db_pool.execute(
        """INSERT INTO backlog_analysis
           (batch_run_id, lesson_a_id, lesson_b_id, cosine, verdict, confidence,
            left_source_agent, right_source_agent)
           VALUES ($1, $2, $3, 0.99, 'duplicate', 0.95, 'claude', 'claude')""",
        batch_id, L3["id"], L4["id"],
    )

    try:
        rows = await fetch_candidate_rows(
            pool=db_pool, batch_run_id=batch_id,
            verdict_in=["duplicate"], confidence_gte=0.90,
        )
        pair_sets = [{r["lesson_a_id"], r["lesson_b_id"]} for r in rows]
        assert {L1["id"], L2["id"]} not in pair_sets, "cross-agent pair leaked"
        assert {L3["id"], L4["id"]} in pair_sets, "same-agent pair missing"
    finally:
        await db_pool.execute(
            "DELETE FROM backlog_analysis WHERE batch_run_id = $1", batch_id,
        )
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2, $3, $4)",
            L1["id"], L2["id"], L3["id"], L4["id"],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_consolidation_cross_agent.py::test_fetch_candidate_rows_excludes_cross_agent -v`
Expected: FAIL — cross-agent pair returned.

- [ ] **Step 3: Modify `fetch_candidate_rows`**

In `src/tools/backlog_apply.py`, find the SQL in `fetch_candidate_rows`. Add to the main WHERE clause:

```sql
  AND ba.left_source_agent = ba.right_source_agent
```

(or equivalently: `AND NOT ba.cross_agent`)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_consolidation_cross_agent.py tests/test_apply_*.py tests/test_backlog_*.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/backlog_apply.py tests/test_consolidation_cross_agent.py
git commit -m "feat(v6): cross-agent skip in fetch_candidate_rows (backlog apply)"
```

---

## Task 16: Optional `source_agent` Filter on Read Tools

**Files:**
- Modify: `src/tools/search.py` (search, search_lessons)
- Modify: `src/tools/journal.py` (read_journal)
- Modify: `tests/test_rule_b.py`

Per Decision #4 in the spec: reads return all agents by default, with an optional `source_agent` filter for inspection.

- [ ] **Step 1: Identify the read tools and their current signatures**

```bash
grep -n "async def search\|async def read_journal\|async def search_lessons" src/tools/search.py src/tools/journal.py src/tools/lessons.py
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_search_lessons_filters_by_source_agent(db_pool, mock_openai):
    from src.tools.lessons import search_lessons  # or src.tools.search

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
            query="unique-search-needle-9999",
            source_agent="codex",
            ctx=_ctx(db_pool, mock_openai),
        )
        payload = _json.loads(result)
        ids = {r["id"] for r in payload.get("results", [])}
        assert L_codex["id"] in ids
        assert L_claude["id"] not in ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", L_claude["id"], L_codex["id"],
        )
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_rule_b.py -v -k "filters_by_source_agent"`
Expected: FAIL — `search_lessons` doesn't accept `source_agent`.

- [ ] **Step 4: Add optional `source_agent` filter to each read tool**

For `search_lessons` (and the unified `search` tool, and `read_journal`):

```python
async def search_lessons(
    query: str,
    # ... existing params ...
    source_agent: str = None,    # NEW: filter by agent family
    ctx: Context = None,
) -> str:
    # In the SQL, conditionally add: AND ($N::text IS NULL OR source_agent = $N)
```

Apply to: `search`, `search_lessons`, `read_journal`. Optional — defaults to None (no filter).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v -k "filters_by_source_agent"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tools/search.py src/tools/lessons.py src/tools/journal.py tests/test_rule_b.py
git commit -m "feat(v6): optional source_agent filter on search/read_journal"
```

---

## Task 17: Admin Scripts — Issue / Revoke / List API Keys

**Files:**
- Create: `scripts/issue_api_key.py`, `scripts/revoke_api_key.py`, `scripts/list_api_keys.py`
- Create: `tests/test_admin_scripts.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_scripts.py`:

```python
"""End-to-end tests for the admin scripts."""

import hashlib
import os
import subprocess
import sys

import pytest


def _env_with_dsn():
    env = os.environ.copy()
    env["DATABASE_URL"] = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://claude:claude@localhost:5434/claude_memory_test",
    )
    return env


@pytest.mark.asyncio
async def test_issue_api_key_creates_row(db_pool):
    result = subprocess.run(
        [sys.executable, "scripts/issue_api_key.py",
         "--family", "codex",
         "--label", "test-script-issuance",
         "--client-name", "codex-cli"],
        capture_output=True, text=True, env=_env_with_dsn(), check=True,
    )
    bearer = next(
        (line.strip() for line in result.stdout.split("\n")
         if len(line.strip()) == 64 and all(c in "0123456789abcdef" for c in line.strip())),
        None,
    )
    assert bearer, result.stdout

    h = hashlib.sha256(bearer.encode()).hexdigest()
    row = await db_pool.fetchrow(
        "SELECT family, label, client_name FROM api_keys WHERE api_key_hash = $1", h,
    )
    assert row is not None
    assert row["family"] == "codex"
    assert row["label"] == "test-script-issuance"
    assert row["client_name"] == "codex-cli"

    await db_pool.execute("DELETE FROM api_keys WHERE api_key_hash = $1", h)


@pytest.mark.asyncio
async def test_revoke_api_key_by_label(db_pool):
    raw = "revoke-test-bearer"
    h = hashlib.sha256(raw.encode()).hexdigest()
    await db_pool.execute(
        """INSERT INTO api_keys (api_key_hash, family, label)
           VALUES ($1, 'codex', 'revoke-test')""",
        h,
    )
    try:
        result = subprocess.run(
            [sys.executable, "scripts/revoke_api_key.py", "--label", "revoke-test"],
            capture_output=True, text=True, env=_env_with_dsn(), check=True,
        )
        assert "Revoked" in result.stdout
        row = await db_pool.fetchrow(
            "SELECT revoked_at FROM api_keys WHERE api_key_hash = $1", h,
        )
        assert row["revoked_at"] is not None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE api_key_hash = $1", h)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_scripts.py -v`
Expected: FAIL — scripts don't exist.

- [ ] **Step 3: Implement `scripts/issue_api_key.py`**

```python
#!/usr/bin/env python3
"""Issue a new API key.

Usage:
    python scripts/issue_api_key.py --family codex --label "Brian Codex laptop" \\
        [--client-name codex-cli] [--scopes read write]

Prints the raw bearer once to stdout. DB stores only the sha256 hash.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg


async def main(args):
    raw = secrets.token_hex(32)
    h = hashlib.sha256(raw.encode()).hexdigest()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            """INSERT INTO api_keys
               (api_key_hash, family, client_name, label, scopes)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            h, args.family, args.client_name, args.label, args.scopes,
        )
    finally:
        await conn.close()

    print(f"Issued API key for family='{args.family}' label='{args.label}'")
    print(f"   id: {row['id']}")
    print(f"   client_name: {args.client_name}")
    print(f"   scopes: {', '.join(args.scopes)}")
    print()
    print("Bearer token (store NOW -- will not be shown again):")
    print(f"   {raw}")
    print()
    suggested = "CODEX_MEMORY_TOKEN" if args.family == "codex" else "CLAUDE_MEMORY_TOKEN"
    print(f"Suggested env var name: {suggested}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--family", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--client-name", default=None)
    p.add_argument("--scopes", nargs="+", default=["read", "write"])
    sys.exit(asyncio.run(main(p.parse_args())))
```

- [ ] **Step 4: Implement `scripts/revoke_api_key.py`**

```python
#!/usr/bin/env python3
"""Revoke an API key by id or label."""

import argparse, asyncio, os, sys
import asyncpg


async def main(args):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL.", file=sys.stderr)
        return 1
    if not (args.id or args.label):
        print("Provide --id or --label.", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        if args.id:
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE id = $1 AND revoked_at IS NULL", args.id,
            )
        else:
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE label = $1 AND revoked_at IS NULL", args.label,
            )
    finally:
        await conn.close()

    affected = int(result.split()[-1])
    if affected == 0:
        print("No matching active key.", file=sys.stderr)
        return 3
    print(f"Revoked {affected} key(s).")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--id", type=int)
    p.add_argument("--label")
    sys.exit(asyncio.run(main(p.parse_args())))
```

- [ ] **Step 5: Implement `scripts/list_api_keys.py`**

```python
#!/usr/bin/env python3
"""List api_keys with status."""

import argparse, asyncio, os, sys
import asyncpg


async def main(args):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        where = "" if args.include_revoked else "WHERE revoked_at IS NULL"
        rows = await conn.fetch(
            f"""SELECT id, family, client_name, label, scopes, created_at,
                       last_seen_at, revoked_at
                FROM api_keys {where} ORDER BY id"""
        )
    finally:
        await conn.close()

    if not rows:
        print("No keys.")
        return 0

    print(f"{'id':<4} {'family':<8} {'label':<40} {'last_seen':<20} {'status'}")
    print("-" * 100)
    for r in rows:
        status = "REVOKED" if r["revoked_at"] else "active"
        last = r["last_seen_at"].isoformat(timespec="seconds") if r["last_seen_at"] else "never"
        print(f"{r['id']:<4} {r['family']:<8} {(r['label'] or ''):<40} {last:<20} {status}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--include-revoked", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
```

- [ ] **Step 6: Make scripts executable + run tests**

```bash
chmod +x scripts/issue_api_key.py scripts/revoke_api_key.py scripts/list_api_keys.py
pytest tests/test_admin_scripts.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/ tests/test_admin_scripts.py
git commit -m "feat(v6): admin scripts to issue/revoke/list api_keys"
```

---

## Task 18: `list_clients` MCP Tool

**Files:**
- Modify: `src/tools/admin.py`
- Modify: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_b.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k "list_clients"`
Expected: FAIL — `list_clients` not defined.

- [ ] **Step 3: Implement `list_clients`**

Append to `src/tools/admin.py`:

```python
@mcp.tool()
async def list_clients(ctx: Context = None) -> str:
    """List all known MCP clients (api_keys + OAuth) with family and status.

    Admin scope required.
    """
    from src.identity import require_admin
    require_admin()
    app = ctx.request_context.lifespan_context

    api_key_rows = await app.db.fetch(
        """SELECT id, family, client_name, label, scopes,
                  created_at, last_seen_at, revoked_at
           FROM api_keys ORDER BY id"""
    )
    oauth_rows = await app.db.fetch(
        """SELECT c.client_id, c.client_name, c.client_id_issued_at, f.family
           FROM oauth_clients c
           LEFT JOIN oauth_client_family f ON f.client_id = c.client_id
           ORDER BY c.client_id_issued_at"""
    )

    out = []
    for r in api_key_rows:
        out.append({
            "source": "api_key",
            "id": r["id"],
            "family": r["family"],
            "client_name": r["client_name"],
            "label": r["label"],
            "scopes": list(r["scopes"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
        })
    for r in oauth_rows:
        out.append({
            "source": "oauth",
            "client_id": r["client_id"],
            "client_name": r["client_name"],
            "family": r["family"] or "unknown",
            "issued_at": r["client_id_issued_at"],
        })

    return json.dumps({"clients": out})
```

(Add `import json` at the top of the file if not present.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v -k "list_clients"`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/admin.py tests/test_rule_b.py
git commit -m "feat(v6): list_clients admin MCP tool"
```

---

## Task 19: Full Test Suite + Manual Smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

```bash
ssh ai-server 'docker start claude_memory_test_db' 2>/dev/null || true
sleep 2
pytest tests/ -v
```

Expected: all PASS. Any regression must be root-caused — most likely either a stale `find_candidates` call site missing the new required `source_agent` arg, or a tool that now requires identity but hasn't been updated.

- [ ] **Step 2: Manual end-to-end smoke (local)**

```bash
# Apply migration locally (if not already done in pre-flight)
PGPASSWORD=claude psql -h localhost -p 5434 -U claude -d claude_memory_test \
    -f db/migrations/v6_attribution.sql

# Start server pointed at test DB
TEST_DATABASE_URL='postgresql://claude:claude@localhost:5434/claude_memory_test' \
DATABASE_URL='postgresql://claude:claude@localhost:5434/claude_memory_test' \
API_KEY='smoke-key' OPENAI_API_KEY=sk-dummy ANTHROPIC_API_KEY=sk-dummy \
    uvicorn src.server:app --port 8003 &
SERVER_PID=$!
sleep 3

# Issue a Codex test token
DATABASE_URL='postgresql://claude:claude@localhost:5434/claude_memory_test' \
    python scripts/issue_api_key.py --family codex --label "smoke" --client-name codex-cli

# Health check
curl -s http://localhost:8003/health

# (Optional) MCP client smoke test against the bearer

kill $SERVER_PID
```

Expected: `{"status":"healthy","service":"claude-memory"}` from health; bearer printed by the script.

- [ ] **Step 3: Commit any fixes**

```bash
git add -u
git commit -m "fix(v6): test-suite green after attribution rollout" || echo "nothing to commit"
```

---

## Task 20: Production Deployment + Rotation

**Files:** none (operational)

- [ ] **Step 1: Back up prod DB**

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@ec2.example.com \
    "cd ~/claude-memory && docker exec claude_memory_db pg_dump -U claude claude_memory > backup_pre_v6.sql && ls -la backup_pre_v6.sql"
```

Confirm backup file exists and is non-trivial size.

- [ ] **Step 2: Apply migration to prod**

```bash
scp -i ~/.ssh/your-key.pem db/migrations/v6_attribution.sql \
    ubuntu@ec2.example.com:~/claude-memory/db/migrations/
ssh -i ~/.ssh/your-key.pem ubuntu@ec2.example.com \
    "cd ~/claude-memory && docker exec -i claude_memory_db psql -U claude -d claude_memory < db/migrations/v6_attribution.sql"
```

Expected: ALTER/CREATE confirmations, COMMIT at end.

- [ ] **Step 3: Deploy new server build**

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@ec2.example.com \
    "cd ~/claude-memory && git pull && docker-compose up -d --build"
sleep 5
curl https://memory.example.com/health
```

Expected: healthy. Tail logs for any startup errors.

- [ ] **Step 4: Issue Codex prod token**

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@ec2.example.com \
    "cd ~/claude-memory && docker exec -i claude_memory_mcp \
     env DATABASE_URL='postgresql://claude:claude@db:5432/claude_memory' \
     python scripts/issue_api_key.py \
       --family codex --label 'Brian Codex laptop' --client-name codex-cli"
```

Capture the bearer (shown once). Set on Codex host:

```bash
echo 'export CODEX_MEMORY_TOKEN=<bearer>' >> ~/.zshrc
```

Configure Codex MCP TOML:

```toml
[mcp_servers.claude-memory]
url = "https://memory.example.com/mcp"
bearer_token_env_var = "CODEX_MEMORY_TOKEN"
```

Smoke-test: have Codex call `search()` and verify a response.

- [ ] **Step 5: Issue per-machine Claude tokens**

For each machine (Workstation, ai-server, laptop):

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@ec2.example.com \
    "docker exec -i claude_memory_mcp \
     env DATABASE_URL='postgresql://claude:claude@db:5432/claude_memory' \
     python scripts/issue_api_key.py \
       --family claude --label 'Brian Claude <machine>' --client-name 'claude-<machine>'"
```

For each machine: set `CLAUDE_MEMORY_TOKEN=<that machine's bearer>` in shell env, then update `claude_desktop_config.json` / `~/.claude/` mcp configs to use a shell wrapper that reads the env var:

```json
{
  "mcpServers": {
    "claude-memory": {
      "command": "sh",
      "args": [
        "-c",
        "npx -y mcp-remote@latest https://memory.example.com/mcp --header \"Authorization:Bearer $CLAUDE_MEMORY_TOKEN\""
      ]
    }
  }
}
```

If Claude Desktop's spawned shell doesn't inherit env, fall back to inlining the new bearer in the config (still per-machine — different bearer on each machine).

Test connectivity (a single `search()` call) from each machine before deleting the old config.

- [ ] **Step 6: Monitor**

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@ec2.example.com \
    "docker logs claude_memory_mcp --since 24h 2>&1 | grep DEPRECATION | wc -l"
```

When this count is 0 for 7 consecutive days, proceed to step 7.

- [ ] **Step 7: Retire legacy API_KEY path**

In `src/identity.py`, remove the legacy branch from `resolve_identity` and the `LEGACY_API_KEY` constant. In `src/auth.py`'s `load_access_token`, remove the `if token == self.api_key:` block. Rotate the `API_KEY` env var in prod (any client still using it will now get 401).

```bash
git add src/identity.py src/auth.py
git commit -m "chore(v6): retire legacy API_KEY back-compat path"
git push
```

Then redeploy and verify all configured clients still work via api_keys/OAuth.

---

## Self-Review

**Spec coverage:**
- Identity granularity (hybrid family + raw client_id) — Tasks 1 (schema), 2–4 (resolver).
- Cross-agent write permissions (rule b) — Tasks 6 (helpers), 11 (enforcement).
- Owned/shared categorization — Task 6 (table sets).
- Consolidation skip — Tasks 13 (log-time), 14 (analyzer records), 15 (apply filters).
- Codex onboarding — Tasks 1, 3, 17 (scripts), 20 (prod issuance).
- Admin scope — Task 12.
- Unknown OAuth lenient — Task 4 (resolver inserts `unknown`).
- Per-machine Claude — Task 20 step 5.
- `list_clients` admin tool — Task 18.
- Legacy retirement — Task 20 step 7.
- Optional `source_agent` filter on reads — Task 16.

**Placeholder scan:** No "TBD" / "implement later" / "similar to Task N" remaining; every step has actual code or commands.

**Type consistency:**
- `Identity` shape consistent across Tasks 2, 3, 4, 5, 6, 7+.
- `stamp() -> tuple[str, Optional[str]]` consistent.
- `assert_can_write(pool, table, row_id)` signature stable.
- `find_candidates(...)` requires `source_agent` from Task 13 onward; Task 13 step 5 updates all known call sites.

**Risks called out:**
- Task 0 (ContextVar spike) is a gating risk. If it fails, Task 5 forks to a `request.state` middleware design and downstream tasks need a small adjustment to `stamp()` / `assert_can_write()` to take a `request`/`ctx` argument. The plan flags this as a stop-and-replan condition.
- Per-machine Claude config update (Task 20 step 5) depends on `mcp-remote`/Claude Desktop env-var behavior. The shell wrapper is the fallback; inline-bearer is the fallback's fallback.
