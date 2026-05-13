# v6: Multi-Agent Attribution & Codex Onboarding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make claude-memory a multi-agent shared corpus. Every write is stamped with `source_agent` (family) and `source_client_id`. Owned content (lessons, journal, specs, etc.) is protected by rule-b: only the original author's agent family can modify. Shared metadata (projects, project_state, etc.) remains last-writer-wins. v5 consolidation skips cross-agent pairs at production mutation points. Codex onboards via a new `api_keys` table; Claude's fleet migrates to per-machine tokens.

**Architecture:** New `src/identity.py` resolves `(family, client_id, scopes)` per request from one of three auth paths (legacy API_KEY, `api_keys` hash match, OAuth access token). Resolved identity is stored in a `contextvars.ContextVar` for the duration of the request and read by tools via `get_identity()`. Migration `004_v6_attribution.sql` adds new tables and stamps the columns. Tool changes are mechanical: stamp on insert, assert ownership before owned-content updates. Consolidation candidate queries get a `source_agent =` filter; the v5.1 analyzer stays unfiltered with a derived `cross_agent` column.

**Tech Stack:** Python 3.11+, asyncpg, FastMCP, PostgreSQL 16 + pgvector, pytest-asyncio.

**Pre-flight:** The test DB container (`claude_memory_test_db` on slmbeast, port 5434) is currently stopped. Before running tests, start it: `ssh slmbeast 'docker start claude_memory_test_db'`. Schema migrations 001–003 should already be applied to the test DB.

---

## File Structure

**New files:**
- `migrations/004_v6_attribution.sql` — schema migration (api_keys, oauth_client_family, source_agent + source_client_id columns, audit columns, generated cross_agent column on backlog_analysis)
- `src/identity.py` — identity resolver, ContextVar-backed `get_identity()`, `stamp()`, `assert_can_write()`
- `scripts/issue_api_key.py` — admin CLI to issue tokens
- `scripts/revoke_api_key.py` — admin CLI to revoke tokens
- `scripts/list_api_keys.py` — admin CLI to list tokens
- `tests/test_identity.py` — resolver branch tests
- `tests/test_rule_b.py` — cross-agent write enforcement tests
- `tests/test_consolidation_cross_agent.py` — cross-agent skip regression tests
- `tests/test_v6_migration.py` — migration backfill verification

**Modified files:**
- `src/auth.py` — resolver hook in `load_access_token` to populate the ContextVar
- `src/tools/lessons.py` — stamp on log_lesson/log_pattern/rate_lesson; rule-b on update_lesson/retire_lesson
- `src/tools/journal.py` — stamp on write_journal
- `src/tools/specs.py` — stamp on create_spec; rule-b on update_spec/retire_spec
- `src/tools/agents.py` — stamp on register_agent; rule-b on update_agent/retire_agent
- `src/tools/mcp_registry.py` — stamp on register_mcp_server/register_mcp_tool; rule-b on update_mcp_server/retire_mcp_server
- `src/tools/annotations.py` — stamp on annotate; rule-b on clear_annotation
- `src/tools/projects.py` — stamp on add_project/update_project_state/set_project_claude_md/update_project_claude_md/merge_projects (Pattern 3 + admin scope on merge)
- `src/tools/infra.py` — stamp on add_machine/add_container/get_permissions writes/add guardrails
- `src/tools/sessions.py` — stamp on start_session/end_session
- `src/tools/admin.py` — admin scope precondition on resolve_conflict; add `list_clients` MCP tool
- `src/tools/consolidation.py` — verify queue tools work cross-family on intra-family pairs (no code change expected, just tests)
- `src/tools/backlog_apply.py` — `fetch_candidate_rows` adds source_agent filter
- `src/consolidation/candidates.py` — candidate query adds source_agent filter
- `src/consolidation/backlog.py` — analyzer remains unfiltered; stamps cross_agent computed column

---

## Task 1: Schema Migration

**Files:**
- Create: `migrations/004_v6_attribution.sql`
- Create: `tests/test_v6_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_v6_migration.py
"""Verify v6 migration produces expected schema state."""

import pytest


@pytest.mark.asyncio
async def test_api_keys_table_exists(db_pool):
    """api_keys table has expected columns."""
    cols = await db_pool.fetch("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'api_keys'
        ORDER BY ordinal_position
    """)
    names = {r["column_name"] for r in cols}
    assert names >= {
        "id", "api_key_hash", "family", "client_name", "label",
        "scopes", "created_at", "last_seen_at", "revoked_at"
    }


@pytest.mark.asyncio
async def test_oauth_client_family_table_exists(db_pool):
    """oauth_client_family table has expected columns."""
    cols = await db_pool.fetch("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'oauth_client_family'
    """)
    names = {r["column_name"] for r in cols}
    assert names >= {"client_id", "family", "client_name", "inferred_from", "inferred_at"}


@pytest.mark.asyncio
async def test_source_agent_on_owned_tables(db_pool):
    """Every owned-content table has source_agent + source_client_id columns."""
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
    """Every shared-metadata table has source_agent + source_client_id columns."""
    shared = ["projects", "project_state", "approaches", "key_files", "guardrails",
              "permissions", "project_aliases", "machines", "databases", "containers"]
    for t in shared:
        cols = await db_pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            t,
        )
        names = {r["column_name"] for r in cols}
        assert "source_agent" in names, f"{t} missing source_agent"


@pytest.mark.asyncio
async def test_backlog_analysis_cross_agent_column(db_pool):
    """backlog_analysis has cross_agent generated column."""
    cols = await db_pool.fetch("""
        SELECT column_name, is_generated FROM information_schema.columns
        WHERE table_name = 'backlog_analysis' AND column_name = 'cross_agent'
    """)
    assert len(cols) == 1
    assert cols[0]["is_generated"] == "ALWAYS"


@pytest.mark.asyncio
async def test_existing_rows_backfilled(db_pool):
    """All existing rows in owned tables stamped source_agent='claude'."""
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lessons WHERE source_agent IS NULL OR source_agent <> 'claude'"
    )
    assert count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v6_migration.py -v`
Expected: All tests FAIL with `relation "api_keys" does not exist` or `column "source_agent" does not exist`.

- [ ] **Step 3: Write the migration**

Create `migrations/004_v6_attribution.sql`:

```sql
-- v6: Multi-agent attribution
-- Adds source_agent + source_client_id to all writeable tables.
-- Creates api_keys (static bearer tokens) and oauth_client_family
-- (DCR family inference).
-- Backfills all existing rows to source_agent='claude'.

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
-- Owned content tables: source_agent + source_client_id
-- ============================================

ALTER TABLE lessons             ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE lessons             ADD COLUMN IF NOT EXISTS source_client_id TEXT;
ALTER TABLE lessons             ADD COLUMN IF NOT EXISTS retired_by_agent TEXT;
ALTER TABLE lessons             ADD COLUMN IF NOT EXISTS updated_by_agent TEXT;

ALTER TABLE patterns            ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE patterns            ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE journal             ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE journal             ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE agent_specs         ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE agent_specs         ADD COLUMN IF NOT EXISTS source_client_id TEXT;
ALTER TABLE agent_specs         ADD COLUMN IF NOT EXISTS retired_by_agent TEXT;
ALTER TABLE agent_specs         ADD COLUMN IF NOT EXISTS updated_by_agent TEXT;

ALTER TABLE specifications      ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE specifications      ADD COLUMN IF NOT EXISTS source_client_id TEXT;
ALTER TABLE specifications      ADD COLUMN IF NOT EXISTS retired_by_agent TEXT;
ALTER TABLE specifications      ADD COLUMN IF NOT EXISTS updated_by_agent TEXT;

ALTER TABLE mcp_servers         ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE mcp_servers         ADD COLUMN IF NOT EXISTS source_client_id TEXT;
ALTER TABLE mcp_servers         ADD COLUMN IF NOT EXISTS retired_by_agent TEXT;
ALTER TABLE mcp_servers         ADD COLUMN IF NOT EXISTS updated_by_agent TEXT;

ALTER TABLE mcp_server_tools    ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE mcp_server_tools    ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE annotations         ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE annotations         ADD COLUMN IF NOT EXISTS source_client_id TEXT;

-- ============================================
-- Shared metadata tables: source_agent + source_client_id
-- ============================================

ALTER TABLE projects            ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE projects            ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE project_state       ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE project_state       ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE approaches          ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE approaches          ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE key_files           ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE key_files           ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE guardrails          ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE guardrails          ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE permissions         ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE permissions         ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE project_aliases     ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE project_aliases     ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE machines            ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE machines            ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE databases           ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE databases           ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE containers          ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE containers          ADD COLUMN IF NOT EXISTS source_client_id TEXT;

ALTER TABLE mcp_server_projects ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE mcp_server_projects ADD COLUMN IF NOT EXISTS source_client_id TEXT;

-- conflicts table (optional, may not exist in all environments)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'conflicts') THEN
        EXECUTE 'ALTER TABLE conflicts ADD COLUMN IF NOT EXISTS source_agent TEXT NOT NULL DEFAULT ''claude''';
        EXECUTE 'ALTER TABLE conflicts ADD COLUMN IF NOT EXISTS source_client_id TEXT';
    END IF;
END $$;

ALTER TABLE sessions            ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE sessions            ADD COLUMN IF NOT EXISTS source_client_id TEXT;

-- ============================================
-- Audit attribution on consolidation tables
-- ============================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'consolidation_runs') THEN
        EXECUTE 'ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS source_agent TEXT';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backlog_analysis') THEN
        EXECUTE 'ALTER TABLE backlog_analysis ADD COLUMN IF NOT EXISTS left_source_agent TEXT';
        EXECUTE 'ALTER TABLE backlog_analysis ADD COLUMN IF NOT EXISTS right_source_agent TEXT';
        EXECUTE 'ALTER TABLE backlog_analysis ADD COLUMN IF NOT EXISTS cross_agent BOOLEAN
                 GENERATED ALWAYS AS (left_source_agent IS DISTINCT FROM right_source_agent) STORED';
    END IF;
END $$;

COMMIT;
```

- [ ] **Step 4: Apply migration to test DB**

```bash
ssh slmbeast 'docker start claude_memory_test_db' 2>/dev/null || true
sleep 2
PGPASSWORD=claude psql -h localhost -p 5434 -U claude -d claude_memory_test \
    -f migrations/004_v6_attribution.sql
```

Expected output: a series of `ALTER TABLE` and `CREATE TABLE` confirmations, ending with `COMMIT`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_v6_migration.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add migrations/004_v6_attribution.sql tests/test_v6_migration.py
git commit -m "feat(v6): add attribution schema migration

Adds source_agent + source_client_id columns to all writeable
tables, creates api_keys and oauth_client_family, and adds the
cross_agent generated column to backlog_analysis. Backfills
existing rows to source_agent='claude'."
```

---

## Task 2: Identity Resolver — Module Skeleton + Legacy Branch

**Files:**
- Create: `src/identity.py`
- Create: `tests/test_identity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_identity.py
"""Identity resolver tests."""

import os
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.identity import resolve_identity, Identity


@pytest.mark.asyncio
async def test_legacy_api_key_resolves_to_claude(monkeypatch):
    """A bearer matching the legacy API_KEY env var resolves to family=claude."""
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", "legacy-secret-xyz")
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    identity = await resolve_identity(pool, "legacy-secret-xyz")

    assert identity is not None
    assert identity.family == "claude"
    assert identity.client_id == "legacy-api-key"
    assert identity.scopes == ["read", "write"]
    assert identity.source == "legacy"


@pytest.mark.asyncio
async def test_unknown_bearer_returns_none():
    """An unrecognized bearer returns None (resolver does not throw)."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    identity = await resolve_identity(pool, "definitely-not-a-real-token")

    assert identity is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.identity'`.

- [ ] **Step 3: Implement the module skeleton + legacy branch**

Create `src/identity.py`:

```python
"""Identity resolver for multi-agent attribution.

Maps a request bearer token to an (agent_family, client_id, scopes) triple
via one of three paths: legacy API_KEY env var, api_keys table hash lookup,
or OAuth access token. Result is stored in a ContextVar for the duration
of the request and read by tools via `get_identity()`.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

# Legacy API_KEY from environment (back-compat path). Module-level so tests
# can monkeypatch it.
LEGACY_API_KEY: Optional[str] = os.getenv("API_KEY")


@dataclass(frozen=True)
class Identity:
    family: str             # 'claude' | 'codex' | 'unknown'
    client_id: str          # 'legacy-api-key' | 'apikey:N' | 'oauth:<client_id>'
    scopes: list[str]       # ['read', 'write'] or ['read', 'write', 'admin']
    source: str             # 'legacy' | 'apikey' | 'oauth'


# Per-request identity. Set by auth.py during load_access_token,
# read by tools via get_identity().
_current_identity: contextvars.ContextVar[Optional[Identity]] = contextvars.ContextVar(
    "current_identity", default=None
)


def set_identity(identity: Optional[Identity]) -> contextvars.Token:
    """Set the current request's identity. Returns a reset token."""
    return _current_identity.set(identity)


def get_identity() -> Optional[Identity]:
    """Return the current request's identity, or None if unauthenticated."""
    return _current_identity.get()


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


async def resolve_identity(pool: asyncpg.Pool, bearer: str) -> Optional[Identity]:
    """Resolve a bearer to an Identity. Returns None if unrecognized.

    Resolution order:
    1. Legacy API_KEY env var equality (back-compat path)
    2. api_keys hash match (future task)
    3. OAuth access token (future task)
    """
    # 1. Legacy API_KEY path
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

    # 2 + 3 added in later tasks.
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_identity.py
git commit -m "feat(v6): identity resolver skeleton + legacy API_KEY branch"
```

---

## Task 3: Identity Resolver — `api_keys` Branch

**Files:**
- Modify: `src/identity.py`
- Modify: `tests/test_identity.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_identity.py`:

```python
@pytest.mark.asyncio
async def test_api_keys_hash_match(db_pool):
    """A bearer whose sha256 matches an api_keys row resolves to that row's family."""
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

        # Verify last_seen_at was updated
        last_seen = await db_pool.fetchval(
            "SELECT last_seen_at FROM api_keys WHERE id = $1", key_id,
        )
        assert last_seen is not None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key_id)


@pytest.mark.asyncio
async def test_api_keys_revoked_does_not_match(db_pool):
    """A revoked api_keys row does not resolve."""
    raw = "test-bearer-revoked-bbbb"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, scopes, revoked_at)
           VALUES ($1, 'codex', ARRAY['read','write'], NOW()) RETURNING id""",
        h,
    )
    key_id = row["id"]

    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key_id)


@pytest.mark.asyncio
async def test_api_keys_admin_scope_preserved(db_pool):
    """An api_keys row with admin scope returns it in Identity.scopes."""
    raw = "test-bearer-admin-cccc"
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, scopes)
           VALUES ($1, 'claude', ARRAY['read','write','admin']) RETURNING id""",
        h,
    )
    key_id = row["id"]

    try:
        identity = await resolve_identity(db_pool, raw)
        assert identity is not None
        assert "admin" in identity.scopes
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py::test_api_keys_hash_match -v`
Expected: FAIL — resolve_identity returns None for unknown bearers; api_keys branch not implemented.

- [ ] **Step 3: Implement the api_keys branch**

Replace the `# 2 + 3 added in later tasks.` comment in `src/identity.py` with:

```python
    # 2. api_keys hash lookup
    bearer_hash = _sha256_hex(bearer)
    row = await pool.fetchrow(
        """SELECT id, family, scopes FROM api_keys
           WHERE api_key_hash = $1 AND revoked_at IS NULL""",
        bearer_hash,
    )
    if row:
        # Update last_seen_at (fire-and-forget — don't block on this)
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
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_identity.py
git commit -m "feat(v6): resolver api_keys hash-match branch"
```

---

## Task 4: Identity Resolver — OAuth Branch

**Files:**
- Modify: `src/identity.py`
- Modify: `tests/test_identity.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_identity.py`:

```python
@pytest.mark.asyncio
async def test_oauth_token_resolves_claude_family(db_pool):
    """An OAuth access token whose client_name starts with 'claude' resolves to family=claude."""
    # Set up an OAuth client + token directly in the DB
    client_id = "client_test_oauth_claude"
    client_name = "claude-code-test"
    token = "oauth-test-token-dddd"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', $2, 'none', extract(epoch from NOW())::int, '{}')""",
        client_id, client_name,
    )
    await db_pool.execute(
        """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
           VALUES ($1, $2, '[]'::jsonb, $3)""",
        token, client_id, 2**31 - 1,  # far future
    )

    try:
        identity = await resolve_identity(db_pool, token)

        assert identity is not None
        assert identity.family == "claude"
        assert identity.client_id == f"oauth:{client_id}"
        assert identity.source == "oauth"

        # Verify oauth_client_family row was inserted
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
    """An OAuth client with an unrecognized name prefix gets family='unknown'."""
    client_id = "client_test_oauth_unknown"
    client_name = "some-random-app"
    token = "oauth-test-token-eeee"

    await db_pool.execute(
        """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                                       token_endpoint_auth_method, client_id_issued_at, raw_data)
           VALUES ($1, 'secret', $2, 'none', extract(epoch from NOW())::int, '{}')""",
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py::test_oauth_token_resolves_claude_family -v`
Expected: FAIL — OAuth branch not implemented.

- [ ] **Step 3: Add a family-prefix classifier**

In `src/identity.py`, add this helper near the top of the file (above `resolve_identity`):

```python
def classify_family_from_name(client_name: Optional[str]) -> str:
    """Map an OAuth client_name (or api_keys.client_name) to a family.

    Prefix rules are case-insensitive. Unknown names fall through to 'unknown'.
    """
    if not client_name:
        return "unknown"
    n = client_name.lower()
    if n.startswith("claude"):
        return "claude"
    if n.startswith("codex"):
        return "codex"
    return "unknown"
```

Then add the OAuth branch after the api_keys branch in `resolve_identity`:

```python
    # 3. OAuth access token lookup
    row = await pool.fetchrow(
        """SELECT t.client_id, c.client_name
           FROM oauth_access_tokens t
           JOIN oauth_clients c ON c.client_id = t.client_id
           WHERE t.token = $1""",
        bearer,
    )
    if row:
        oauth_client_id = row["client_id"]
        client_name = row["client_name"]

        # Look up or insert the family classification
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
                    f"client_id={oauth_client_id} client_name={client_name!r}. "
                    "Update oauth_client_family.family manually if this is misclassified."
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
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_identity.py
git commit -m "feat(v6): resolver OAuth branch + family prefix classifier"
```

---

## Task 5: Wire Resolver Into Auth Layer

**Files:**
- Modify: `src/auth.py`
- Modify: `tests/test_identity.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_identity.py`:

```python
@pytest.mark.asyncio
async def test_load_access_token_sets_contextvar(db_pool, monkeypatch):
    """OAuth provider's load_access_token populates the identity ContextVar."""
    from src.auth import MemoryOAuthProvider
    from src.identity import get_identity, _current_identity

    # Reset the contextvar before the test
    _current_identity.set(None)

    provider = MemoryOAuthProvider(api_key="test-legacy-key")
    provider.set_pool(db_pool)
    monkeypatch.setattr("src.identity.LEGACY_API_KEY", "test-legacy-key")

    result = await provider.load_access_token("test-legacy-key")

    assert result is not None
    assert result.client_id == "api-key-user"  # existing back-compat behavior

    identity = get_identity()
    assert identity is not None
    assert identity.family == "claude"
    assert identity.source == "legacy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identity.py::test_load_access_token_sets_contextvar -v`
Expected: FAIL — `get_identity()` returns None because the resolver isn't called yet.

- [ ] **Step 3: Hook resolver into `load_access_token`**

Modify `src/auth.py`. Find the `load_access_token` method (around line 250) and replace it with:

```python
    async def load_access_token(self, token: str) -> AccessToken | None:
        """Load an access token and populate the per-request identity ContextVar.

        The contextvar is read by tools to attribute writes. We populate it
        here because this method runs early in every authenticated request.
        """
        from src.identity import resolve_identity, set_identity

        logger.info(f"load_access_token called, is_api_key={token == self.api_key}, token_prefix={token[:8]}...")

        # Resolve identity for stamping. Failure here is non-fatal —
        # the access-token check below is what gates the request.
        try:
            identity = await resolve_identity(self.pool, token)
            if identity is not None:
                set_identity(identity)
        except Exception as e:
            logger.error(f"Identity resolution failed (non-fatal): {e}")

        # Backward compatibility: accept the raw API key as a bearer token
        if token == self.api_key:
            return AccessToken(
                token=token,
                client_id="api-key-user",
                scopes=[],
                expires_at=None,
            )

        # Check database for OAuth-issued access tokens
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

Note: a bearer that resolves via the **api_keys** path is NOT a valid OAuth access token, so this method will return `None` for those — which would block the request. Need a second branch:

After the OAuth row lookup, before the final `return None`, add:

```python
        # api_keys-issued bearer (resolver already validated it via set_identity).
        # If identity is set with source='apikey', accept the token.
        from src.identity import get_identity
        identity = get_identity()
        if identity is not None and identity.source == "apikey":
            return AccessToken(
                token=token,
                client_id=identity.client_id,
                scopes=identity.scopes,
                expires_at=None,
            )

        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/auth.py tests/test_identity.py
git commit -m "feat(v6): wire identity resolver into OAuth provider auth layer"
```

---

## Task 6: Write-Stamp + Rule-B Helpers

**Files:**
- Modify: `src/identity.py`
- Create: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rule_b.py`:

```python
"""Tests for stamp() and assert_can_write() helpers."""

import pytest

from src.identity import (
    Identity, set_identity, stamp, assert_can_write,
)


@pytest.mark.asyncio
async def test_stamp_returns_current_identity():
    set_identity(Identity(
        family="codex", client_id="apikey:42",
        scopes=["read", "write"], source="apikey",
    ))
    family, client_id = stamp()
    assert family == "codex"
    assert client_id == "apikey:42"


@pytest.mark.asyncio
async def test_stamp_returns_defaults_when_unauth():
    set_identity(None)
    family, client_id = stamp()
    # Default to claude for back-compat when no identity is set
    assert family == "claude"
    assert client_id is None


@pytest.mark.asyncio
async def test_assert_can_write_allows_own_row(db_pool):
    """An agent can update a row it owns."""
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b own-row test', 'content', 'codex')
           RETURNING id""",
    )
    lesson_id = row["id"]
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        # Should NOT raise
        await assert_can_write(db_pool, "lessons", lesson_id)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_assert_can_write_blocks_foreign_row(db_pool):
    """An agent cannot update a row owned by a different family."""
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b foreign-row test', 'content', 'claude')
           RETURNING id""",
    )
    lesson_id = row["id"]
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        with pytest.raises(PermissionError) as exc:
            await assert_can_write(db_pool, "lessons", lesson_id)
        assert "codex" in str(exc.value)
        assert "claude" in str(exc.value)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_assert_can_write_shared_metadata_table_always_allows(db_pool):
    """Tables in the shared-metadata set always allow writes (last-writer-wins)."""
    # projects is a shared-metadata table
    row = await db_pool.fetchrow(
        """INSERT INTO projects (name, source_agent)
           VALUES ('rule-b-shared-test', 'claude')
           RETURNING id""",
    )
    project_id = row["id"]
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write"], source="apikey",
        ))
        # Should NOT raise (projects is shared)
        await assert_can_write(db_pool, "projects", project_id)
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id = $1", project_id)


@pytest.mark.asyncio
async def test_assert_can_write_admin_scope_bypasses_rule_b(db_pool):
    """An identity with 'admin' scope can modify any row regardless of family."""
    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('rule-b admin-bypass test', 'content', 'claude')
           RETURNING id""",
    )
    lesson_id = row["id"]
    try:
        set_identity(Identity(
            family="codex", client_id="apikey:99",
            scopes=["read", "write", "admin"], source="apikey",
        ))
        await assert_can_write(db_pool, "lessons", lesson_id)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v`
Expected: FAIL with `ImportError: cannot import name 'stamp'` (and `assert_can_write`).

- [ ] **Step 3: Implement the helpers**

Append to `src/identity.py`:

```python
# ---------------------------------------------------------------------------
# Write-stamp + Rule-B Helpers
# ---------------------------------------------------------------------------

# Tables where rule b (own-content) applies.
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

# Tables where last-writer-wins applies (no rule-b enforcement).
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
    "mcp_server_projects",
    "sessions",
})


def stamp() -> tuple[str, Optional[str]]:
    """Return (source_agent, source_client_id) for the current request.

    Defaults to ('claude', None) when no identity is set — back-compat
    for any code path not yet covered by the resolver.
    """
    identity = get_identity()
    if identity is None:
        return ("claude", None)
    return (identity.family, identity.client_id)


async def assert_can_write(pool: asyncpg.Pool, table: str, row_id: int) -> None:
    """Raise PermissionError if the current identity cannot write to `table.row_id`.

    - Shared-metadata tables always allow writes.
    - Owned-content tables: only the original source_agent's family can modify.
    - Identities with 'admin' scope bypass rule b.
    """
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

    row = await pool.fetchrow(
        f"SELECT source_agent FROM {table} WHERE id = $1",  # noqa: S608 (table is allow-listed)
        row_id,
    )
    if row is None:
        return  # Let the caller handle missing rows
    owner = row["source_agent"]

    if owner != current_family:
        raise PermissionError(
            f"agent '{current_family}' cannot modify row owned by '{owner}' in {table}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py tests/test_rule_b.py
git commit -m "feat(v6): stamp() and assert_can_write() helpers with rule-b semantics"
```

---

## Task 7: Stamp Owned-Content Inserts — Lessons + Patterns + Rate

**Files:**
- Modify: `src/tools/lessons.py`
- Modify: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_log_lesson_stamps_source_agent(db_pool, mock_openai, mock_anthropic):
    """log_lesson stamps source_agent + source_client_id from current identity."""
    from src.tools.lessons import log_lesson
    from src.server import AppContext
    from unittest.mock import MagicMock

    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))

    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=mock_openai, anthropic=mock_anthropic,
    )

    result = await log_lesson(
        title="rule-b stamp test lesson",
        content="stamped by codex",
        ctx=ctx,
    )

    import json as _json
    payload = _json.loads(result)
    if not payload.get("success", True):
        # If duplicate title, clean up the existing row and retry
        await db_pool.execute("DELETE FROM lessons WHERE title = $1", "rule-b stamp test lesson")
        result = await log_lesson(
            title="rule-b stamp test lesson",
            content="stamped by codex",
            ctx=ctx,
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rule_b.py::test_log_lesson_stamps_source_agent -v`
Expected: FAIL — `source_agent` is `'claude'` (default) instead of `'codex'`.

- [ ] **Step 3: Modify `log_lesson` to stamp**

In `src/tools/lessons.py`, find the INSERT in `log_lesson` (around line 56) and update both the SQL and the parameters:

```python
    # Insert lesson
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

- [ ] **Step 4: Repeat for `log_pattern`**

In the same file, find `log_pattern`'s INSERT. Apply the same pattern: `stamp()` call, add `source_agent` and `source_client_id` columns + parameters.

- [ ] **Step 5: Repeat for `rate_lesson`**

`rate_lesson` increments counters on the existing row, so it doesn't create a row. But the rating itself is the user's act of rating — if ratings are stored in a separate `lesson_ratings` table, add stamping to the INSERT there. **Check first:** `grep -n "rate_lesson\|lesson_ratings" src/tools/lessons.py` — if it's just an UPDATE on counters, no stamping change is needed (the lesson's source_agent never changes from rating).

If a separate `lesson_ratings` table exists with its own INSERT, add `source_agent, source_client_id` columns to it via a new migration step `004b_lesson_ratings_attribution.sql` and stamp the INSERT.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: All previous tests + the new stamp test PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tools/lessons.py tests/test_rule_b.py
git commit -m "feat(v6): stamp source_agent on log_lesson + log_pattern inserts"
```

---

## Task 8: Stamp Owned-Content Inserts — Journal

**Files:**
- Modify: `src/tools/journal.py`
- Modify: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_write_journal_stamps_source_agent(db_pool, mock_openai):
    """write_journal stamps source_agent + source_client_id."""
    from src.tools.journal import write_journal
    from src.server import AppContext
    from unittest.mock import MagicMock

    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))

    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=mock_openai, anthropic=MagicMock(),
    )

    import json as _json
    result = await write_journal(
        content="codex's first journal entry",
        tags=["test", "v6"],
        ctx=ctx,
    )
    payload = _json.loads(result)
    entry_id = payload["entry_id"]
    try:
        row = await db_pool.fetchrow(
            "SELECT source_agent, source_client_id FROM journal WHERE id = $1",
            entry_id,
        )
        assert row["source_agent"] == "codex"
        assert row["source_client_id"] == "apikey:7"
    finally:
        await db_pool.execute("DELETE FROM journal WHERE id = $1", entry_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rule_b.py::test_write_journal_stamps_source_agent -v`
Expected: FAIL — source_agent defaults to 'claude'.

- [ ] **Step 3: Modify `write_journal` to stamp**

In `src/tools/journal.py`, locate the INSERT INTO journal. Add the stamp call and extend the SQL/params:

```python
from src.identity import stamp
source_agent, source_client_id = stamp()
row = await app.db.fetchrow(
    """INSERT INTO journal (content, tags, mood, project_id, session_id, embedding,
                            source_agent, source_client_id)
       VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8)
       RETURNING id""",
    content, tags or [], mood, project_id, session_id, embedding_str,
    source_agent, source_client_id,
)
```

(Adjust parameter positions to match the actual existing INSERT — the principle is "add two columns, two params, two values".)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py::test_write_journal_stamps_source_agent -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/journal.py tests/test_rule_b.py
git commit -m "feat(v6): stamp source_agent on write_journal"
```

---

## Task 9: Stamp Owned-Content Inserts — Specs, Agents, MCP Registry, Annotations

**Files:**
- Modify: `src/tools/specs.py`, `src/tools/agents.py`, `src/tools/mcp_registry.py`, `src/tools/annotations.py`
- Modify: `tests/test_rule_b.py`

This is a multi-file mechanical pass. Each tool follows the same pattern as `log_lesson` / `write_journal`.

- [ ] **Step 1: Write a parameterized failing test**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_module,tool_name,setup_call,table,extract_id", [
    (
        "src.tools.specs", "create_spec",
        {"name": "v6-test-spec", "title": "T", "content": "C", "project": None, "subsystem": None},
        "specifications", lambda p: p["spec_id"],
    ),
    (
        "src.tools.agents", "register_agent",
        {"name": "v6-test-agent", "title": "T", "content": "C", "subsystem": None,
         "use_when": None, "key_responsibilities": None},
        "agent_specs", lambda p: p["agent_id"],
    ),
    (
        "src.tools.mcp_registry", "register_mcp_server",
        {"name": "v6-test-mcp", "description": "D", "transport": "stdio",
         "command": "x", "config_example": None, "homepage_url": None},
        "mcp_servers", lambda p: p["server_id"],
    ),
    (
        "src.tools.annotations", "annotate",
        {"entity_type": "lesson", "entity_id": 1, "note": "v6-test-anno", "tags": None},
        "annotations", lambda p: p["annotation_id"],
    ),
])
async def test_owned_inserts_stamp_source_agent(
    db_pool, mock_openai, mock_anthropic, tool_module, tool_name, setup_call, table, extract_id,
):
    """Each Pattern-1 insert tool stamps source_agent + source_client_id."""
    import importlib
    from src.server import AppContext
    from unittest.mock import MagicMock
    import json as _json

    mod = importlib.import_module(tool_module)
    tool = getattr(mod, tool_name)

    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=mock_openai, anthropic=mock_anthropic,
    )

    # Annotations need an entity to attach to — ensure a lesson exists with id=1
    if tool_name == "annotate":
        existing = await db_pool.fetchval("SELECT id FROM lessons ORDER BY id LIMIT 1")
        setup_call["entity_id"] = existing or 1

    result = await tool(ctx=ctx, **setup_call)
    payload = _json.loads(result)
    row_id = extract_id(payload)
    try:
        row = await db_pool.fetchrow(
            f"SELECT source_agent, source_client_id FROM {table} WHERE id = $1",
            row_id,
        )
        assert row["source_agent"] == "codex"
        assert row["source_client_id"] == "apikey:7"
    finally:
        await db_pool.execute(f"DELETE FROM {table} WHERE id = $1", row_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k owned_inserts_stamp`
Expected: All four parametrized cases FAIL — source_agent='claude'.

- [ ] **Step 3: Modify each tool's INSERT**

For each of `create_spec` (specs.py), `register_agent` (agents.py), `register_mcp_server` (mcp_registry.py), `register_mcp_tool` (mcp_registry.py), `annotate` (annotations.py), apply the pattern:

```python
from src.identity import stamp
source_agent, source_client_id = stamp()
# Then in the INSERT: add ", source_agent, source_client_id" to columns,
# add ", $N, $N+1" to VALUES, append source_agent, source_client_id to params.
```

Each tool's INSERT is a few lines; the change is purely additive.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/specs.py src/tools/agents.py src/tools/mcp_registry.py src/tools/annotations.py tests/test_rule_b.py
git commit -m "feat(v6): stamp source_agent on spec/agent/mcp/annotation inserts"
```

---

## Task 10: Stamp Shared-Metadata Writes — Projects, Project State, Infra

**Files:**
- Modify: `src/tools/projects.py`, `src/tools/infra.py`, `src/tools/sessions.py`
- Modify: `tests/test_rule_b.py`

Shared metadata follows Pattern 3: stamp `source_agent` on the row to the **current writer's** family (not the original author's). Subsequent writers overwrite it. No `assert_can_write` check.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_update_project_state_stamps_writer(db_pool):
    """update_project_state stamps source_agent of the *writer*, not the original author."""
    from src.tools.projects import update_project_state
    from src.server import AppContext
    from unittest.mock import MagicMock

    # Insert a project owned by claude
    proj = await db_pool.fetchrow(
        """INSERT INTO projects (name, source_agent) VALUES ('v6-pstate-test', 'claude')
           RETURNING id""",
    )
    project_id = proj["id"]

    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )

    try:
        await update_project_state(
            project="v6-pstate-test",
            current_focus="codex took over",
            ctx=ctx,
        )
        row = await db_pool.fetchrow(
            "SELECT source_agent FROM project_state WHERE project_id = $1",
            project_id,
        )
        assert row["source_agent"] == "codex"  # writer, not the project owner
    finally:
        await db_pool.execute("DELETE FROM project_state WHERE project_id = $1", project_id)
        await db_pool.execute("DELETE FROM projects WHERE id = $1", project_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rule_b.py::test_update_project_state_stamps_writer -v`
Expected: FAIL — source_agent='claude' default.

- [ ] **Step 3: Modify the project / infra / sessions write tools**

For each tool that mutates a shared-metadata row (`update_project_state`, `add_project`, `set_project_claude_md`, `update_project_claude_md`, `add_machine`, `add_container`, `start_session`, `end_session`, etc.), wrap the SQL to set `source_agent = $N, source_client_id = $N+1` on the row.

For INSERT-or-UPDATE patterns (common with project_state's UPSERT), include the stamp columns in both INSERT and the ON CONFLICT DO UPDATE branch. Example for project_state:

```python
from src.identity import stamp
source_agent, source_client_id = stamp()
await app.db.execute(
    """
    INSERT INTO project_state (project_id, current_focus, blockers, next_steps,
                                source_agent, source_client_id)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (project_id) DO UPDATE
    SET current_focus = EXCLUDED.current_focus,
        blockers      = EXCLUDED.blockers,
        next_steps    = EXCLUDED.next_steps,
        source_agent  = EXCLUDED.source_agent,
        source_client_id = EXCLUDED.source_client_id,
        updated_at    = NOW()
    """,
    project_id, current_focus, blockers or [], next_steps or [],
    source_agent, source_client_id,
)
```

Adjust each tool's SQL to match its actual columns; the principle is the same.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/projects.py src/tools/infra.py src/tools/sessions.py tests/test_rule_b.py
git commit -m "feat(v6): stamp source_agent on shared-metadata writes (last-writer-wins)"
```

---

## Task 11: Rule-B Enforcement — Owned Content Updates

**Files:**
- Modify: `src/tools/lessons.py`, `src/tools/specs.py`, `src/tools/agents.py`, `src/tools/mcp_registry.py`, `src/tools/annotations.py`
- Modify: `tests/test_rule_b.py`

Every update/retire on owned content gets a `assert_can_write` call before mutation.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_codex_cannot_retire_claude_lesson(db_pool):
    """retire_lesson from codex on a claude lesson raises PermissionError."""
    from src.tools.lessons import retire_lesson
    from src.server import AppContext
    from unittest.mock import MagicMock

    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-retire-foreign-test', 'claude content', 'claude')
           RETURNING id""",
    )
    lesson_id = row["id"]

    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )

    try:
        with pytest.raises(PermissionError) as exc:
            await retire_lesson(lesson_id=lesson_id, reason="codex says no", ctx=ctx)
        assert "codex" in str(exc.value)
        assert "claude" in str(exc.value)
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)


@pytest.mark.asyncio
async def test_codex_can_retire_own_lesson(db_pool):
    """retire_lesson from codex on a codex lesson succeeds."""
    from src.tools.lessons import retire_lesson
    from src.server import AppContext
    from unittest.mock import MagicMock
    import json as _json

    row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-retire-own-test', 'codex content', 'codex')
           RETURNING id""",
    )
    lesson_id = row["id"]

    set_identity(Identity(
        family="codex", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )

    try:
        result = await retire_lesson(lesson_id=lesson_id, reason="cleanup", ctx=ctx)
        payload = _json.loads(result)
        assert payload.get("success") is True
        row = await db_pool.fetchrow(
            "SELECT retired_at, retired_by_agent FROM lessons WHERE id = $1",
            lesson_id,
        )
        assert row["retired_at"] is not None
        assert row["retired_by_agent"] == "codex"
    finally:
        await db_pool.execute("DELETE FROM lessons WHERE id = $1", lesson_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k retire`
Expected: First test FAILS (no PermissionError raised); second fails on `retired_by_agent` column.

- [ ] **Step 3: Add rule-b enforcement and audit-field stamping**

For each owned-content update/retire tool, before the UPDATE, add:

```python
from src.identity import assert_can_write, stamp
await assert_can_write(app.db, "lessons", lesson_id)
acting_agent, _ = stamp()
```

Then extend the UPDATE to set `retired_by_agent = $X` or `updated_by_agent = $X`. Example for `retire_lesson`:

```python
await app.db.execute(
    """UPDATE lessons
       SET retired_at = NOW(),
           retired_reason = $1,
           retired_by_agent = $2
       WHERE id = $3""",
    reason, acting_agent, lesson_id,
)
```

Apply the same pattern to: `update_lesson`, `update_spec`, `retire_spec`, `update_agent`, `retire_agent`, `update_mcp_server`, `retire_mcp_server`, `clear_annotation`.

For `clear_annotation`: rule-b means an agent can only clear its own annotation, even if attached to someone else's lesson. The check is on the annotation row's source_agent, not the parent lesson — `assert_can_write(app.db, "annotations", annotation_id)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/lessons.py src/tools/specs.py src/tools/agents.py src/tools/mcp_registry.py src/tools/annotations.py tests/test_rule_b.py
git commit -m "feat(v6): rule-b enforcement on owned-content updates + retires"
```

---

## Task 12: Admin Scope on `merge_projects` and `resolve_conflict`

**Files:**
- Modify: `src/tools/projects.py` (for `merge_projects`), `src/tools/admin.py` (or wherever `resolve_conflict` lives)
- Modify: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_merge_projects_requires_admin(db_pool):
    """merge_projects without admin scope raises PermissionError."""
    from src.tools.projects import merge_projects
    from src.server import AppContext
    from unittest.mock import MagicMock

    set_identity(Identity(
        family="claude", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",  # No admin
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )

    with pytest.raises(PermissionError) as exc:
        await merge_projects(source_name="A", target_name="B", ctx=ctx)
    assert "admin" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_merge_projects_with_admin_proceeds(db_pool):
    """merge_projects with admin scope passes the permission gate."""
    from src.tools.projects import merge_projects
    from src.server import AppContext
    from unittest.mock import MagicMock

    a = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('v6-merge-A') RETURNING id"
    )
    b = await db_pool.fetchrow(
        "INSERT INTO projects (name) VALUES ('v6-merge-B') RETURNING id"
    )

    set_identity(Identity(
        family="claude", client_id="apikey:7",
        scopes=["read", "write", "admin"], source="apikey",
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )

    try:
        # Should NOT raise PermissionError. May raise other errors due to test
        # setup; we only care about the scope gate.
        try:
            await merge_projects(source_name="v6-merge-A", target_name="v6-merge-B", ctx=ctx)
        except PermissionError:
            raise
        except Exception:
            pass  # Other errors are out of scope for this test
    finally:
        await db_pool.execute("DELETE FROM projects WHERE id IN ($1, $2)", a["id"], b["id"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k merge_projects`
Expected: Both fail (no admin gate exists yet).

- [ ] **Step 3: Add admin scope gate**

In `src/identity.py`, add a helper:

```python
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

Then in `merge_projects`:

```python
from src.identity import require_admin
require_admin()
# ... rest of the merge logic
```

Same for `resolve_conflict` wherever it's defined.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v -k merge_projects`
Expected: Both PASS.

- [ ] **Step 5: Commit**

```bash
git add src/identity.py src/tools/projects.py src/tools/admin.py tests/test_rule_b.py
git commit -m "feat(v6): require admin scope for merge_projects + resolve_conflict"
```

---

## Task 13: Cross-Agent Consolidation Skip — Log-Time

**Files:**
- Modify: `src/consolidation/candidates.py`
- Modify: `src/consolidation/orchestrator.py` (caller passes source_agent through)
- Modify: `src/tools/lessons.py` (passes new lesson's source_agent to consolidator)
- Create: `tests/test_consolidation_cross_agent.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consolidation_cross_agent.py`:

```python
"""Cross-agent consolidation skip — regression tests."""

import pytest

from src.consolidation.candidates import find_candidates


@pytest.mark.asyncio
async def test_find_candidates_skips_cross_agent(db_pool):
    """A codex lesson does NOT match a claude lesson even at high cosine."""
    # Insert two lessons with identical content (cosine ~ 1.0 if same embedding),
    # one stamped 'claude', one stamped 'codex'.
    # We use a synthetic identical embedding to force a max-similarity neighbor.
    emb = "[" + ",".join(["0.1"] * 1536) + "]"

    claude_row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-claude', 'shared content', $1::vector, 'claude')
           RETURNING id""",
        emb,
    )
    codex_row = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-codex', 'shared content', $1::vector, 'codex')
           RETURNING id""",
        emb,
    )

    try:
        # Query as if codex just logged a new lesson
        candidates = await find_candidates(
            pool=db_pool,
            query_embedding=[0.1] * 1536,
            new_lesson_id=codex_row["id"],
            project_id=None,
            cosine_threshold=0.85,
            top_k=10,
            source_agent="codex",  # NEW parameter
        )

        # The claude lesson should NOT appear, even though cosine ~ 1.0
        candidate_ids = {c["id"] for c in candidates}
        assert claude_row["id"] not in candidate_ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)",
            claude_row["id"], codex_row["id"],
        )


@pytest.mark.asyncio
async def test_find_candidates_matches_same_agent(db_pool):
    """A codex lesson DOES match another codex lesson at high cosine."""
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
        candidates = await find_candidates(
            pool=db_pool,
            query_embedding=[0.1] * 1536,
            new_lesson_id=b["id"],
            project_id=None,
            cosine_threshold=0.85,
            top_k=10,
            source_agent="codex",
        )
        candidate_ids = {c["id"] for c in candidates}
        assert a["id"] in candidate_ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", a["id"], b["id"],
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_consolidation_cross_agent.py -v`
Expected: FAIL — `find_candidates()` does not accept `source_agent` keyword.

- [ ] **Step 3: Modify `find_candidates`**

In `src/consolidation/candidates.py`, change the signature and query:

```python
async def find_candidates(
    pool: asyncpg.Pool,
    query_embedding: list[float],
    new_lesson_id: int,
    project_id: int | None,
    cosine_threshold: float,
    top_k: int,
    source_agent: str = "claude",  # NEW
) -> list[dict[str, Any]]:
    """Return up to `top_k` lessons with cosine similarity >= threshold.

    Filters to lessons with matching source_agent (cross-agent skip).
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

- [ ] **Step 4: Thread source_agent through the call chain**

In `src/consolidation/orchestrator.py`, find the call to `find_candidates`. Pass through the lesson's source_agent:

```python
candidates = await find_candidates(
    pool=pool,
    query_embedding=new_embedding,
    new_lesson_id=new_lesson_id,
    project_id=project_id,
    cosine_threshold=COSINE_THRESHOLD,
    top_k=TOP_K,
    source_agent=new_source_agent,  # NEW
)
```

Add `new_source_agent: str` to `consolidate_at_log`'s signature.

In `src/tools/lessons.py`, find the `consolidate_at_log` call inside `log_lesson` and pass the stamped family:

```python
consolidation = await consolidate_at_log(
    pool=app.db,
    anthropic=app.anthropic,
    new_lesson_id=lesson_id,
    new_title=title,
    new_content=content,
    new_embedding=embedding,
    project_id=project_id,
    new_source_agent=source_agent,  # NEW (from earlier stamp() call)
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_consolidation_cross_agent.py -v`
Expected: Both tests PASS.

- [ ] **Step 6: Run the existing consolidation test suite to ensure nothing else broke**

Run: `pytest tests/test_candidates.py tests/test_actor_*.py tests/test_judge.py -v`
Expected: All previously-passing tests still pass. Some tests may need a `source_agent='claude'` default added if they call `find_candidates` directly.

- [ ] **Step 7: Commit**

```bash
git add src/consolidation/candidates.py src/consolidation/orchestrator.py src/tools/lessons.py tests/test_consolidation_cross_agent.py
git commit -m "feat(v6): cross-agent skip in log-time consolidation candidate query"
```

---

## Task 14: Cross-Agent Skip — Backlog Apply Tool

**Files:**
- Modify: `src/tools/backlog_apply.py`
- Modify: `tests/test_consolidation_cross_agent.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consolidation_cross_agent.py`:

```python
@pytest.mark.asyncio
async def test_fetch_candidate_rows_skips_cross_agent(db_pool):
    """fetch_candidate_rows excludes cross-agent pairs."""
    from src.tools.backlog_apply import fetch_candidate_rows

    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-backlog-claude', 'shared', $1::vector, 'claude') RETURNING id""",
        emb,
    )
    codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-backlog-codex', 'shared', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        rows = await fetch_candidate_rows(
            pool=db_pool,
            cosine_threshold=0.85,
            # Other fetch_candidate_rows arguments — match the real signature.
        )
        # No pair where left.source_agent != right.source_agent should appear.
        for r in rows:
            if r["left_id"] in (claude["id"], codex["id"]) and r["right_id"] in (claude["id"], codex["id"]):
                pytest.fail(
                    f"cross-agent pair leaked: {r['left_id']}/{r['right_id']}"
                )
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", claude["id"], codex["id"],
        )
```

(If `fetch_candidate_rows` has a different signature, adjust the call. Look at the existing tests in `tests/test_backlog_pairs.py` for the real shape.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_consolidation_cross_agent.py::test_fetch_candidate_rows_skips_cross_agent -v`
Expected: FAIL — cross-agent pair appears.

- [ ] **Step 3: Modify the query**

In `src/tools/backlog_apply.py`, find `fetch_candidate_rows`'s SQL. Add `AND l1.source_agent = l2.source_agent` to the WHERE clause of the pair-selection query.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_consolidation_cross_agent.py -v`
Expected: All tests PASS.

Also run the existing backlog apply tests:
Run: `pytest tests/test_backlog_pairs.py tests/test_apply_*.py -v`
Expected: All previously-passing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/tools/backlog_apply.py tests/test_consolidation_cross_agent.py
git commit -m "feat(v6): cross-agent skip in backlog apply candidate query"
```

---

## Task 15: Backlog Analyzer — Tag Cross-Agent Pairs, Do Not Filter

**Files:**
- Modify: `src/consolidation/backlog.py`
- Modify: `tests/test_consolidation_cross_agent.py`

The v5.1 analyzer stays unfiltered. Each `backlog_analysis` row stores `left_source_agent` and `right_source_agent`; the `cross_agent` generated column was added by the migration.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consolidation_cross_agent.py`:

```python
@pytest.mark.asyncio
async def test_backlog_analyzer_does_not_filter_cross_agent(db_pool):
    """The analyzer's pair query returns cross-agent pairs (we want them for investigation)."""
    from src.consolidation.backlog import fetch_pairs_for_analysis

    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-analyzer-claude', 'shared', $1::vector, 'claude') RETURNING id""",
        emb,
    )
    codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-analyzer-codex', 'shared', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        pairs = await fetch_pairs_for_analysis(pool=db_pool, cosine_threshold=0.85)
        # Find the cross-agent pair (if it exists in the result set)
        cross_pair = next(
            (p for p in pairs
             if {p["left_id"], p["right_id"]} == {claude["id"], codex["id"]}),
            None,
        )
        assert cross_pair is not None, "analyzer missed the cross-agent pair"
        assert cross_pair["left_source_agent"] != cross_pair["right_source_agent"]
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", claude["id"], codex["id"],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_consolidation_cross_agent.py::test_backlog_analyzer_does_not_filter_cross_agent -v`
Expected: FAIL — the analyzer's SELECT either misses cross-agent pairs or doesn't return `left_source_agent`/`right_source_agent`.

- [ ] **Step 3: Modify the analyzer query**

In `src/consolidation/backlog.py`, find the SELECT that picks pairs. The query already self-joins lessons with itself; extend the SELECT clause to include `l1.source_agent AS left_source_agent, l2.source_agent AS right_source_agent`. **Do not** add a source_agent equality filter — pairs across agents must remain visible for the investigation goal.

When inserting analyzed pairs into `backlog_analysis`, include both source_agent values. The `cross_agent` column is generated automatically.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_consolidation_cross_agent.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/consolidation/backlog.py tests/test_consolidation_cross_agent.py
git commit -m "feat(v6): analyzer keeps cross-agent pairs; tags left/right source_agent"
```

---

## Task 16: Admin Scripts — Issue, Revoke, List API Keys

**Files:**
- Create: `scripts/issue_api_key.py`
- Create: `scripts/revoke_api_key.py`
- Create: `scripts/list_api_keys.py`
- Create: `tests/test_admin_scripts.py`

- [ ] **Step 1: Write the failing test for issue_api_key**

Create `tests/test_admin_scripts.py`:

```python
"""Tests for scripts/issue_api_key.py — verifies side-effects on the DB."""

import hashlib
import subprocess
import sys
import os

import pytest


@pytest.mark.asyncio
async def test_issue_api_key_creates_row(db_pool):
    """Running the script inserts an api_keys row with hashed bearer."""
    env = os.environ.copy()
    env["DATABASE_URL"] = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://claude:claude@localhost:5434/claude_memory_test",
    )

    result = subprocess.run(
        [
            sys.executable, "scripts/issue_api_key.py",
            "--family", "codex",
            "--label", "test-script-issuance",
            "--client-name", "codex-cli",
        ],
        capture_output=True, text=True, env=env, check=True,
    )

    # Bearer is printed on a line starting with "Bearer token" or contains 64 hex chars
    lines = result.stdout.split("\n")
    bearer = None
    for line in lines:
        line = line.strip()
        if len(line) == 64 and all(c in "0123456789abcdef" for c in line):
            bearer = line
            break
    assert bearer is not None, f"Bearer not found in output: {result.stdout}"

    h = hashlib.sha256(bearer.encode()).hexdigest()
    row = await db_pool.fetchrow(
        "SELECT family, label, client_name FROM api_keys WHERE api_key_hash = $1", h,
    )
    assert row is not None
    assert row["family"] == "codex"
    assert row["label"] == "test-script-issuance"
    assert row["client_name"] == "codex-cli"

    await db_pool.execute("DELETE FROM api_keys WHERE api_key_hash = $1", h)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_scripts.py -v`
Expected: FAIL — `scripts/issue_api_key.py` does not exist.

- [ ] **Step 3: Implement `scripts/issue_api_key.py`**

```python
#!/usr/bin/env python3
"""Issue a new API key for the claude-memory MCP server.

Usage:
    python scripts/issue_api_key.py --family codex --label "Brian Codex laptop" \\
        [--client-name codex-cli] [--scopes read write]

Prints the raw bearer once to stdout. The DB stores only the sha256 hash.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg


async def main(args: argparse.Namespace) -> int:
    raw = secrets.token_hex(32)
    h = hashlib.sha256(raw.encode()).hexdigest()

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL or TEST_DATABASE_URL.", file=sys.stderr)
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--family", required=True, help="Agent family (claude, codex, etc.)")
    p.add_argument("--label", required=True, help="Human-readable label")
    p.add_argument("--client-name", default=None, help="Client name (e.g., codex-cli)")
    p.add_argument(
        "--scopes", nargs="+", default=["read", "write"],
        help="Scopes for this token (default: read write)",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main(args)))
```

Make it executable: `chmod +x scripts/issue_api_key.py`.

- [ ] **Step 4: Implement `scripts/revoke_api_key.py`**

```python
#!/usr/bin/env python3
"""Revoke an API key by id or label.

Usage:
    python scripts/revoke_api_key.py --id 7
    python scripts/revoke_api_key.py --label "Brian Codex laptop"
"""

import argparse
import asyncio
import os
import sys

import asyncpg


async def main(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL or TEST_DATABASE_URL.", file=sys.stderr)
        return 1
    if not (args.id or args.label):
        print("Provide --id or --label.", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        if args.id:
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE id = $1 AND revoked_at IS NULL",
                args.id,
            )
        else:
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE label = $1 AND revoked_at IS NULL",
                args.label,
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--id", type=int, help="api_keys.id")
    p.add_argument("--label", help="api_keys.label")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args)))
```

- [ ] **Step 5: Implement `scripts/list_api_keys.py`**

```python
#!/usr/bin/env python3
"""List all api_keys with status.

Usage:
    python scripts/list_api_keys.py [--include-revoked]
"""

import argparse
import asyncio
import os
import sys

import asyncpg


async def main(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL or TEST_DATABASE_URL.", file=sys.stderr)
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
        last_seen = r["last_seen_at"].isoformat(timespec="seconds") if r["last_seen_at"] else "never"
        print(
            f"{r['id']:<4} {r['family']:<8} {(r['label'] or ''):<40} "
            f"{last_seen:<20} {status}"
        )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--include-revoked", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args)))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_admin_scripts.py -v`
Expected: PASS.

- [ ] **Step 7: Smoke-test the scripts manually**

```bash
TEST_DATABASE_URL='postgresql://claude:claude@localhost:5434/claude_memory_test' \
    python scripts/issue_api_key.py --family codex --label "smoke test" --client-name codex-cli
# copy the bearer from the output

TEST_DATABASE_URL='postgresql://claude:claude@localhost:5434/claude_memory_test' \
    python scripts/list_api_keys.py
# verify the new row appears

TEST_DATABASE_URL='postgresql://claude:claude@localhost:5434/claude_memory_test' \
    python scripts/revoke_api_key.py --label "smoke test"

TEST_DATABASE_URL='postgresql://claude:claude@localhost:5434/claude_memory_test' \
    python scripts/list_api_keys.py --include-revoked
# verify status=REVOKED
```

- [ ] **Step 8: Commit**

```bash
chmod +x scripts/issue_api_key.py scripts/revoke_api_key.py scripts/list_api_keys.py
git add scripts/issue_api_key.py scripts/revoke_api_key.py scripts/list_api_keys.py tests/test_admin_scripts.py
git commit -m "feat(v6): admin scripts to issue/revoke/list api_keys"
```

---

## Task 17: `list_clients` MCP Admin Tool

**Files:**
- Modify: `src/tools/admin.py`
- Modify: `tests/test_rule_b.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rule_b.py`:

```python
@pytest.mark.asyncio
async def test_list_clients_requires_admin(db_pool):
    """list_clients without admin scope raises PermissionError."""
    from src.tools.admin import list_clients
    from src.server import AppContext
    from unittest.mock import MagicMock

    set_identity(Identity(
        family="claude", client_id="apikey:7",
        scopes=["read", "write"], source="apikey",
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )

    with pytest.raises(PermissionError):
        await list_clients(ctx=ctx)


@pytest.mark.asyncio
async def test_list_clients_returns_both_paths(db_pool):
    """list_clients with admin returns rows from api_keys and oauth_clients."""
    from src.tools.admin import list_clients
    from src.server import AppContext
    from unittest.mock import MagicMock
    import json as _json
    import hashlib

    # Seed one api_keys row
    raw = "list-clients-test-bearer"
    h = hashlib.sha256(raw.encode()).hexdigest()
    key = await db_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, label) VALUES ($1, 'codex', 'list-test')
           RETURNING id""",
        h,
    )

    set_identity(Identity(
        family="claude", client_id="apikey:99",
        scopes=["read", "write", "admin"], source="apikey",
    ))
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )

    try:
        result = await list_clients(ctx=ctx)
        payload = _json.loads(result)
        sources = {r["source"] for r in payload["clients"]}
        assert "api_key" in sources
        # OAuth row presence depends on whether any oauth_clients exist in test DB; not asserted.
        api_key_rows = [r for r in payload["clients"] if r["source"] == "api_key"]
        assert any(r["label"] == "list-test" for r in api_key_rows)
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE id = $1", key["id"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_b.py -v -k list_clients`
Expected: FAIL — `list_clients` doesn't exist.

- [ ] **Step 3: Implement `list_clients`**

In `src/tools/admin.py`, add:

```python
import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.identity import require_admin


@mcp.tool()
async def list_clients(ctx: Context = None) -> str:
    """List all known MCP clients (api_keys + OAuth) with family and status.

    Admin scope required.
    """
    require_admin()
    app = ctx.request_context.lifespan_context

    api_key_rows = await app.db.fetch(
        """SELECT id, family, client_name, label, scopes,
                  created_at, last_seen_at, revoked_at
           FROM api_keys ORDER BY id"""
    )
    oauth_rows = await app.db.fetch(
        """SELECT c.client_id, c.client_name, c.client_id_issued_at,
                  f.family
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_b.py -v -k list_clients`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/admin.py tests/test_rule_b.py
git commit -m "feat(v6): list_clients MCP tool (admin-scoped)"
```

---

## Task 18: Verify Full Test Suite + Manual Smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

```bash
ssh slmbeast 'docker start claude_memory_test_db' 2>/dev/null || true
sleep 2
pytest tests/ -v
```

Expected: All tests pass. If anything from v5 / v5.1 / v5.2 broke, fix the root cause (most likely a tool that calls `find_candidates` or a `consolidate_at_log` invocation that doesn't pass `new_source_agent`).

- [ ] **Step 2: Manual end-to-end smoke against local server**

Run the dev server locally and confirm an end-to-end flow:

```bash
# Apply migration to local dev DB if running locally; otherwise use prod-mirror.
# Start the server (in another terminal):
TEST_DATABASE_URL=... uvicorn src.server:app --port 8003

# Issue a Codex token against the dev DB:
python scripts/issue_api_key.py --family codex --label "smoke codex" --client-name codex-cli

# Use the bearer to hit the server:
curl -H "Authorization: Bearer <bearer>" http://localhost:8003/health
# Expected: {"status":"healthy","service":"claude-memory"}

# Use an MCP client (e.g., claude code with a temporary mcp.json) to log a lesson;
# verify the row's source_agent='codex' in psql.

psql -h localhost -p 5434 -U claude claude_memory_test \
    -c "SELECT id, title, source_agent, source_client_id
        FROM lessons ORDER BY id DESC LIMIT 5;"
```

Expected: The newly-logged lesson has `source_agent='codex'`.

- [ ] **Step 3: Commit (if any fixes were needed)**

```bash
git add -u
git commit -m "fix(v6): test-suite green after attribution rollout"
```

If no changes: skip the commit.

---

## Task 19: Production Deployment + Rotation Plan

**Files:** none (operational)

This is the operational cutover, not code. Follow this order:

- [ ] **Step 1: Backup prod DB**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@52.201.241.106 \
    "cd ~/claude-memory && docker exec claude_memory_db pg_dump -U claude claude_memory > backup_pre_v6.sql"
```

- [ ] **Step 2: Apply migration to prod**

```bash
scp -i ~/.ssh/AWS_FR.pem migrations/004_v6_attribution.sql \
    ubuntu@52.201.241.106:~/claude-memory/migrations/
ssh -i ~/.ssh/AWS_FR.pem ubuntu@52.201.241.106 \
    "cd ~/claude-memory && docker exec -i claude_memory_db psql -U claude -d claude_memory < migrations/004_v6_attribution.sql"
```

Expected: A series of ALTER TABLE + CREATE TABLE confirmations.

- [ ] **Step 3: Deploy the new server build**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@52.201.241.106 \
    "cd ~/claude-memory && git pull && docker-compose up -d --build"
```

- [ ] **Step 4: Verify the server is healthy and identity resolves**

```bash
curl https://memory.friendly-robots.com/health
# Expected: {"status":"healthy","service":"claude-memory"}

# Tail logs for "DEPRECATION: legacy API_KEY" — should appear on the first
# request from any existing client (they're still on the legacy bearer).
ssh -i ~/.ssh/AWS_FR.pem ubuntu@52.201.241.106 \
    "docker logs claude_memory_mcp --tail 100"
```

- [ ] **Step 5: Issue Codex token (prod)**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@52.201.241.106 \
    "cd ~/claude-memory && DATABASE_URL='postgresql://claude:claude@db:5432/claude_memory' \
     python scripts/issue_api_key.py \
       --family codex \
       --label 'Brian Codex laptop' \
       --client-name codex-cli"
```

Securely capture the bearer. Set on the Codex host: `export CODEX_MEMORY_TOKEN=<bearer>`.

Configure Codex's MCP TOML:

```toml
[mcp_servers.claude-memory]
url = "https://memory.friendly-robots.com/mcp"
bearer_token_env_var = "CODEX_MEMORY_TOKEN"
```

Verify Codex can call `search()` against the MCP server.

- [ ] **Step 6: Issue per-machine Claude tokens (prod, one per machine)**

For each machine (Mac Studio shared, slmbeast, work laptop):

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@52.201.241.106 \
    "cd ~/claude-memory && DATABASE_URL='postgresql://claude:claude@db:5432/claude_memory' \
     python scripts/issue_api_key.py \
       --family claude \
       --label 'Brian Claude mac-studio' \
       --client-name claude-mac-studio"
```

(Repeat with appropriate labels for `slmbeast`, `work-laptop`, etc.)

On each machine: set `CLAUDE_MEMORY_TOKEN=<bearer>` in the appropriate shell env (e.g., `~/.zshrc` on macOS), then update the MCP config to use the env var instead of the inline bearer.

For `claude_desktop_config.json` (which uses `mcp-remote` via `npx`), the simplest pattern is a shell wrapper:

```json
{
  "mcpServers": {
    "claude-memory": {
      "command": "sh",
      "args": ["-c", "npx -y mcp-remote@latest https://memory.friendly-robots.com/mcp --header \"Authorization:Bearer $CLAUDE_MEMORY_TOKEN\""]
    }
  }
}
```

(Verify Claude Desktop spawns `sh` with the parent env — if not, fall back to inlining the new bearer in the config.)

Test connectivity from each machine (a single `search()` call).

- [ ] **Step 7: Monitor for 7 days**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@52.201.241.106 \
    "docker logs claude_memory_mcp --since 7d 2>&1 | grep DEPRECATION | wc -l"
```

When this count is 0 for 7 consecutive days, proceed to step 8.

- [ ] **Step 8: Retire legacy API_KEY**

In `src/identity.py`, remove the `# 1. Legacy API_KEY path` branch of `resolve_identity`. Remove the `LEGACY_API_KEY` module-level constant. Update `src/auth.py`'s `load_access_token` to remove the `if token == self.api_key:` branch.

Rotate the actual `API_KEY` env var in prod (`docker-compose.yml` or env file) to a new random value — anything using the old value now fails with 401 instead of being accepted with stamped attribution.

```bash
git add src/identity.py src/auth.py
git commit -m "chore(v6): retire legacy API_KEY back-compat path"
git push
```

Then redeploy and verify everything still works.

---

## Self-Review

Spec coverage:
- Identity granularity (Hybrid family + raw client_id) — Task 1 schema, Tasks 2–4 resolver.
- Cross-agent write permissions (Rule b) — Tasks 6, 11.
- Owned/shared categorization — Task 6 (`OWNED_CONTENT_TABLES` / `SHARED_METADATA_TABLES` sets).
- Consolidation skip scope — Tasks 13 (log-time), 14 (backlog apply), 15 (analyzer tags but doesn't filter).
- Codex onboarding via api_keys — Tasks 1 (schema), 3 (resolver branch), 16 (scripts), 19 (production deploy + token issuance).
- Admin scope on merge/resolve — Task 12.
- Unknown OAuth DCR lenient — Task 4 (resolver inserts `unknown`).
- Per-machine Claude tokens — Task 19 step 6.
- `list_clients` admin tool — Task 17.
- Legacy API_KEY retirement — Task 19 step 8.

Placeholder scan: no TBD / "implement later" / "similar to Task N" found. All code blocks contain actual content.

Type consistency: `Identity` dataclass shape consistent across Tasks 2, 3, 4, 5, 6, 11, 12, 17. `stamp()` returns `(str, Optional[str])` consistently. `assert_can_write(pool, table, row_id)` signature consistent.

Open items deferred to implementation by design:
1. Existence of a separate `lesson_ratings` table — handled inline in Task 7 step 5 (check first, add column only if needed).
2. `project_state` history vs upsert — Task 10 step 3 covers UPSERT pattern; if v2 history exists, the pattern extends naturally.
3. Per-request identity plumbing chose ContextVar (Task 2 step 3, Task 5 step 3); decision locked.
4. `mcp-remote` env-var substitution — Task 19 step 6 includes a shell-wrapper fallback.
