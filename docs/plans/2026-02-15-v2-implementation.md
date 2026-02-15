# Claude Memory v2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Sync repo with production, restructure into modules, add project CLAUDE.md, lesson lifecycle, and project name normalization.

**Architecture:** Light restructure of monolithic server.py into src/tools/ modules + shared helpers. Deployed OAuth code becomes the baseline. New features added as new tool modules with a numbered SQL migration.

**Tech Stack:** Python 3.11, FastMCP, asyncpg, pgvector, OpenAI ada-002, PostgreSQL 16, Docker Compose

---

### Task 1: Database Backup

**Files:**
- Create: `backups/` directory (gitignored)

**Step 1: SSH to EC2 and dump the database**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 \
  "docker exec claude_memory_db pg_dump -U claude -d claude_memory --format=custom -f /tmp/claude_memory_backup_20260215.dump"
```

**Step 2: Copy dump to local machine**

```bash
mkdir -p backups
scp -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119:/tmp/claude_memory_backup_20260215.dump backups/
```

**Step 3: Verify dump integrity**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 \
  "docker exec claude_memory_db pg_restore --list /tmp/claude_memory_backup_20260215.dump | head -30"
```

Expected: Table of contents listing tables (machines, projects, lessons, journal, etc.)

**Step 4: Add backups/ to .gitignore**

Append `backups/` to `.gitignore` if not already present.

**Step 5: Commit**

```bash
git add .gitignore
git commit -m "chore: add backups/ to gitignore for database dumps"
```

---

### Task 2: Sync Production Code to Repo

**Files:**
- Modify: `src/server.py` (replace with deployed version)

**Step 1: Copy deployed server.py into repo**

The deployed server.py has already been pulled to `/tmp/ec2_server.py`. Copy it into the repo:

```bash
cp /tmp/ec2_server.py src/server.py
```

**Step 2: Verify no other files have drifted**

requirements.txt and docker-compose.yml are confirmed identical. Only server.py differs.

**Step 3: Commit the sync**

```bash
git add src/server.py
git commit -m "sync: bring deployed OAuth code into repo

Syncs the production server.py (with OAuth 2.0 for Claude Desktop/Mobile)
back into the git repo. This is the deployed code as of 2026-02-12.

Key additions vs repo:
- MemoryOAuthProvider class (in-memory OAuth 2.0)
- /approve authorization page for OAuth flow
- /health and /ready as custom routes
- Backward-compatible API key auth via load_access_token
- Simplified ASGI app setup (mcp.streamable_http_app())"
```

---

### Task 3: Create Module Structure

**Files:**
- Create: `src/config.py`
- Create: `src/db.py`
- Create: `src/auth.py`
- Create: `src/tools/__init__.py`
- Create: `src/tools/search.py`
- Create: `src/tools/projects.py`
- Create: `src/tools/infra.py`
- Create: `src/tools/lessons.py`
- Create: `src/tools/sessions.py`
- Create: `src/tools/journal.py`
- Create: `src/tools/admin.py`
- Modify: `src/server.py` (slim down to app setup + imports)

This is a pure refactor. Every tool stays exactly as-is, just moved to its own file.

**Step 1: Create `src/config.py`**

Extract environment variables and security settings:

```python
import os
from mcp.server.transport_security import TransportSecuritySettings

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://claude:claude@localhost:5432/claude_memory")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
API_KEY = os.getenv("CLAUDE_MEMORY_API_KEY", "dev-key")
ISSUER_URL = os.getenv("OAUTH_ISSUER_URL", "https://memory.friendly-robots.com")

security_settings = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "localhost:8003",
        "127.0.0.1:8003",
        "memory.friendly-robots.com",
        "memory.friendly-robots.com:80",
        "memory.friendly-robots.com:443",
        "memory.friendly-robots.com:*",
    ]
)
```

**Step 2: Create `src/db.py`**

Extract embedding helpers:

```python
from openai import AsyncOpenAI


async def get_embedding(openai: AsyncOpenAI, text: str) -> list[float]:
    """Generate embedding for text using OpenAI ada-002."""
    response = await openai.embeddings.create(
        model="text-embedding-ada-002",
        input=text
    )
    return response.data[0].embedding


def format_embedding(embedding: list[float]) -> str:
    """Format embedding as PostgreSQL vector string."""
    return f"[{','.join(str(x) for x in embedding)}]"
```

**Step 3: Create `src/auth.py`**

Move the entire `MemoryOAuthProvider` class and the `/approve` route handler from server.py. The class is self-contained (~170 lines). The `/approve` custom route stays in server.py since it needs the `mcp` instance, but the provider class moves here.

```python
import secrets
import time
import logging

from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)


class MemoryOAuthProvider:
    # ... (entire class exactly as deployed, ~170 lines)
```

**Step 4: Create `src/tools/__init__.py`**

Empty file.

**Step 5: Create tool modules**

Each tool module follows this pattern:

```python
# src/tools/search.py
import json
from mcp.server.fastmcp import Context
from src.server import mcp
from src.db import get_embedding, format_embedding


@mcp.tool()
async def search(query: str, limit: int = 5, ctx: Context = None) -> str:
    # ... exact existing code
```

Split tools into modules:
- `search.py`: `search`, `search_lessons`
- `projects.py`: `get_project`, `list_projects`
- `infra.py`: `get_connectivity`, `list_machines`, `add_machine`, `add_container`
- `lessons.py`: `log_lesson`, `log_pattern`
- `sessions.py`: `start_session`, `end_session`
- `journal.py`: `write_journal`, `read_journal`
- `admin.py`: `update_project_state`, `check_guardrails`, `add_project`, `get_permissions`

**Step 6: Slim down `src/server.py`**

Server.py becomes:

```python
"""Claude Memory MCP Server - Application setup and configuration."""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import asyncpg
from openai import AsyncOpenAI
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, RedirectResponse

from src.config import DATABASE_URL, OPENAI_API_KEY, API_KEY, ISSUER_URL, security_settings
from src.auth import MemoryOAuthProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    db: asyncpg.Pool
    openai: AsyncOpenAI


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    try:
        yield AppContext(db=pool, openai=openai_client)
    finally:
        await pool.close()


oauth_provider = MemoryOAuthProvider(API_KEY)

auth_settings = AuthSettings(
    issuer_url=ISSUER_URL,
    resource_server_url=f"{ISSUER_URL}/mcp",
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=True),
)

mcp = FastMCP(
    "Claude Memory",
    lifespan=app_lifespan,
    stateless_http=True,
    json_response=True,
    transport_security=security_settings,
    auth=auth_settings,
    auth_server_provider=oauth_provider,
)


# Custom routes
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "claude-memory"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ready")


@mcp.custom_route("/approve", methods=["GET", "POST"])
async def approve_authorization(request: Request):
    # ... OAuth approval page (exact existing code)


# Import tool modules to register them
import src.tools.search
import src.tools.projects
import src.tools.infra
import src.tools.lessons
import src.tools.sessions
import src.tools.journal
import src.tools.admin


app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.server:app", host="0.0.0.0", port=8003, reload=False, log_level="info", access_log=True)
```

**Step 7: Test locally**

```bash
cd /Users/bhengen/dev/claude-memory
python -c "from src.server import app; print('Import OK')"
```

Expected: "Import OK" (no circular import errors)

**Step 8: Commit**

```bash
git add src/
git commit -m "refactor: split server.py into modules

Pure refactor - no behavior changes. Splits the 1401-line server.py into:
- src/config.py (env vars, security settings)
- src/db.py (embedding helpers)
- src/auth.py (OAuth provider)
- src/tools/ (one module per tool category)
- src/server.py (app setup, routes, imports)"
```

---

### Task 4: Create Migration Infrastructure

**Files:**
- Create: `migrations/001_initial_schema.sql`
- Create: `migrations/002_v2_features.sql`

**Step 1: Copy current schema as migration 001**

```bash
cp db/schema.sql migrations/001_initial_schema.sql
```

Add a comment header: `-- Migration 001: Initial schema (reference only, already applied)`

**Step 2: Create migration 002**

```sql
-- Migration 002: v2 features
-- Applied: 2026-02-15
-- Description: Project CLAUDE.md, lesson lifecycle, project aliases

-- Project CLAUDE.md
ALTER TABLE projects ADD COLUMN IF NOT EXISTS claude_md TEXT;

-- Lesson lifecycle (soft delete)
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS retired_at TIMESTAMP;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS retired_reason TEXT;

-- Project name normalization
CREATE TABLE IF NOT EXISTS project_aliases (
    id SERIAL PRIMARY KEY,
    alias VARCHAR(100) UNIQUE NOT NULL,
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_project_aliases_alias ON project_aliases(LOWER(alias));
CREATE INDEX IF NOT EXISTS idx_lessons_retired ON lessons(retired_at) WHERE retired_at IS NULL;
```

**Step 3: Commit**

```bash
git add migrations/
git commit -m "chore: add migration infrastructure with v2 schema changes"
```

---

### Task 5: Add Project Resolution Helper

**Files:**
- Create: `src/helpers.py`

**Step 1: Create the shared project resolver**

```python
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
```

**Step 2: Commit**

```bash
git add src/helpers.py
git commit -m "feat: add case-insensitive project resolution with alias support"
```

---

### Task 6: Add Project CLAUDE.md Tools

**Files:**
- Modify: `src/tools/projects.py`

**Step 1: Add `get_project_claude_md` tool**

```python
@mcp.tool()
async def get_project_claude_md(project: str, ctx: Context = None) -> str:
    """
    Get the CLAUDE.md content for a project.
    Returns the project's philosophy, scope, conventions, and active context.

    Args:
        project: Project name
    """
    app = ctx.request_context.lifespan_context
    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    row = await app.db.fetchrow(
        "SELECT name, claude_md FROM projects WHERE id = $1",
        project_id
    )

    return json.dumps({
        "project": row["name"],
        "claude_md": row["claude_md"],
        "has_claude_md": row["claude_md"] is not None
    })
```

**Step 2: Add `set_project_claude_md` tool**

```python
@mcp.tool()
async def set_project_claude_md(project: str, content: str, ctx: Context = None) -> str:
    """
    Create or fully replace the CLAUDE.md content for a project.

    Args:
        project: Project name
        content: Full CLAUDE.md content (markdown)
    """
    app = ctx.request_context.lifespan_context
    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    await app.db.execute(
        "UPDATE projects SET claude_md = $1, updated_at = NOW() WHERE id = $2",
        content, project_id
    )

    return json.dumps({"success": True, "message": f"CLAUDE.md set for {project}"})
```

**Step 3: Add `update_project_claude_md` tool**

```python
import re

@mcp.tool()
async def update_project_claude_md(
    project: str,
    section: str,
    content: str,
    ctx: Context = None
) -> str:
    """
    Update a specific section of a project's CLAUDE.md by heading match.
    If the section exists, replaces it. If not, appends it.

    Args:
        project: Project name
        section: Section heading to find/replace (e.g., "## Architecture")
        content: New content for this section (include the heading)
    """
    app = ctx.request_context.lifespan_context
    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    row = await app.db.fetchrow(
        "SELECT claude_md FROM projects WHERE id = $1",
        project_id
    )

    existing = row["claude_md"] or ""

    # Find the section by heading level and replace up to the next heading of same or higher level
    heading_match = re.match(r'^(#{1,6})\s', section)
    if not heading_match:
        return json.dumps({"error": "Section must start with a markdown heading (e.g., '## Architecture')"})

    level = len(heading_match.group(1))
    # Pattern: from this heading to the next heading of same or higher level, or end of string
    pattern = re.compile(
        rf'^{re.escape(section)}.*?(?=^#{{1,{level}}}\s|\Z)',
        re.MULTILINE | re.DOTALL
    )

    if pattern.search(existing):
        updated = pattern.sub(content.rstrip() + '\n\n', existing, count=1)
        action = "updated"
    else:
        updated = existing.rstrip() + '\n\n' + content.rstrip() + '\n'
        action = "appended"

    await app.db.execute(
        "UPDATE projects SET claude_md = $1, updated_at = NOW() WHERE id = $2",
        updated, project_id
    )

    return json.dumps({"success": True, "message": f"Section '{section}' {action} in CLAUDE.md for {project}"})
```

**Step 4: Commit**

```bash
git add src/tools/projects.py
git commit -m "feat: add project CLAUDE.md tools (get, set, update by section)"
```

---

### Task 7: Add Lesson Lifecycle Tools

**Files:**
- Modify: `src/tools/lessons.py`
- Modify: `src/tools/search.py`

**Step 1: Add `update_lesson` tool to lessons.py**

```python
@mcp.tool()
async def update_lesson(
    lesson_id: int,
    title: str = None,
    content: str = None,
    tags: list[str] = None,
    severity: str = None,
    ctx: Context = None
) -> str:
    """
    Update an existing lesson. Only provided fields are changed.
    Regenerates embedding if title or content changes.

    Args:
        lesson_id: ID of the lesson to update
        title: New title (optional)
        content: New content (optional)
        tags: New tags (optional)
        severity: New severity (optional)
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow("SELECT * FROM lessons WHERE id = $1", lesson_id)
    if not existing:
        return json.dumps({"error": f"Lesson {lesson_id} not found"})

    updates = []
    params = []
    param_idx = 1

    if title is not None:
        updates.append(f"title = ${param_idx}")
        params.append(title)
        param_idx += 1

    if content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(content)
        param_idx += 1

    if tags is not None:
        updates.append(f"tags = ${param_idx}")
        params.append(tags)
        param_idx += 1

    if severity is not None:
        updates.append(f"severity = ${param_idx}")
        params.append(severity)
        param_idx += 1

    if not updates:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if title or content changed
    if title is not None or content is not None:
        new_title = title if title is not None else existing["title"]
        new_content = content if content is not None else existing["content"]
        embedding = await get_embedding(app.openai, f"{new_title}\n{new_content}")
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(lesson_id)
    await app.db.execute(
        f"UPDATE lessons SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({"success": True, "message": f"Lesson {lesson_id} updated"})
```

**Step 2: Add `retire_lesson` tool to lessons.py**

```python
@mcp.tool()
async def retire_lesson(
    lesson_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire a lesson (soft delete). Retired lessons are excluded from search by default.

    Args:
        lesson_id: ID of the lesson to retire
        reason: Why this lesson is being retired (optional)
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow("SELECT id, title FROM lessons WHERE id = $1", lesson_id)
    if not existing:
        return json.dumps({"error": f"Lesson {lesson_id} not found"})

    await app.db.execute(
        "UPDATE lessons SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, lesson_id
    )

    return json.dumps({
        "success": True,
        "message": f"Lesson {lesson_id} ('{existing['title']}') retired"
    })
```

**Step 3: Update search tools to exclude retired lessons**

In `src/tools/search.py`, update `search()`:
- Add `WHERE l.retired_at IS NULL` to the lessons subquery in the `semantic_search` call.

Since `search()` uses the `semantic_search` database function, we need to either:
- Update the database function, OR
- Add the filter in the application layer

Simplest: update the `semantic_search` function in migration 002 to filter retired lessons:

Add to `migrations/002_v2_features.sql`:

```sql
-- Update semantic_search to exclude retired lessons
CREATE OR REPLACE FUNCTION semantic_search(
    query_embedding VECTOR(1536),
    search_limit INT DEFAULT 5
)
RETURNS TABLE (
    source_type TEXT,
    source_id INT,
    title TEXT,
    content TEXT,
    similarity FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM (
        SELECT
            'lesson'::TEXT as source_type,
            l.id as source_id,
            l.title::TEXT as title,
            l.content::TEXT as content,
            1 - (l.embedding <=> query_embedding) as similarity
        FROM lessons l
        WHERE l.embedding IS NOT NULL AND l.retired_at IS NULL

        UNION ALL

        SELECT
            'pattern'::TEXT as source_type,
            p.id as source_id,
            p.name::TEXT as title,
            p.problem::TEXT as content,
            1 - (p.embedding <=> query_embedding) as similarity
        FROM patterns p
        WHERE p.embedding IS NOT NULL

        UNION ALL

        SELECT
            'session'::TEXT as source_type,
            s.id as source_id,
            'Session ' || s.id::TEXT as title,
            s.summary::TEXT as content,
            1 - (s.embedding <=> query_embedding) as similarity
        FROM sessions s
        WHERE s.embedding IS NOT NULL

        UNION ALL

        SELECT
            'journal'::TEXT as source_type,
            j.id as source_id,
            'Journal ' || to_char(j.entry_date, 'YYYY-MM-DD')::TEXT as title,
            j.content::TEXT as content,
            1 - (j.embedding <=> query_embedding) as similarity
        FROM journal j
        WHERE j.embedding IS NOT NULL
    ) combined
    ORDER BY similarity DESC
    LIMIT search_limit;
END;
$$ LANGUAGE plpgsql;
```

In `search_lessons`, add `AND l.retired_at IS NULL` to the WHERE clause, and add optional `include_retired` parameter.

**Step 4: Commit**

```bash
git add src/tools/lessons.py src/tools/search.py migrations/002_v2_features.sql
git commit -m "feat: add lesson update/retire with search filtering"
```

---

### Task 8: Add Project Merge Tool

**Files:**
- Modify: `src/tools/admin.py`

**Step 1: Add `merge_projects` tool**

```python
@mcp.tool()
async def merge_projects(
    keep: str,
    merge: str,
    ctx: Context = None
) -> str:
    """
    Merge one project into another. Reassigns all associated data
    (lessons, sessions, journal entries, key_files, approaches, state)
    from the merge project to the keep project. Creates an alias so
    the old name still resolves. Deletes the merge project record.

    Args:
        keep: Project name to keep (canonical)
        merge: Project name to merge into keep (will be deleted)
    """
    app = ctx.request_context.lifespan_context

    keep_row = await app.db.fetchrow("SELECT id, name FROM projects WHERE LOWER(name) = LOWER($1)", keep)
    merge_row = await app.db.fetchrow("SELECT id, name FROM projects WHERE LOWER(name) = LOWER($1)", merge)

    if not keep_row:
        return json.dumps({"error": f"Keep project '{keep}' not found"})
    if not merge_row:
        return json.dumps({"error": f"Merge project '{merge}' not found"})
    if keep_row["id"] == merge_row["id"]:
        return json.dumps({"error": "Cannot merge a project into itself"})

    keep_id = keep_row["id"]
    merge_id = merge_row["id"]
    moved = {}

    # Reassign lessons
    result = await app.db.execute(
        "UPDATE lessons SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["lessons"] = int(result.split()[-1])

    # Reassign sessions
    result = await app.db.execute(
        "UPDATE sessions SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["sessions"] = int(result.split()[-1])

    # Reassign journal entries
    result = await app.db.execute(
        "UPDATE journal SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["journal_entries"] = int(result.split()[-1])

    # Reassign key_files
    result = await app.db.execute(
        "UPDATE key_files SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["key_files"] = int(result.split()[-1])

    # Reassign approaches
    result = await app.db.execute(
        "UPDATE approaches SET project_id = $1 WHERE project_id = $2", keep_id, merge_id
    )
    moved["approaches"] = int(result.split()[-1])

    # Delete merge project's state (keep project's state wins)
    await app.db.execute("DELETE FROM project_state WHERE project_id = $1", merge_id)

    # Create alias for old name
    await app.db.execute(
        """
        INSERT INTO project_aliases (alias, project_id)
        VALUES ($1, $2)
        ON CONFLICT (alias) DO NOTHING
        """,
        merge_row["name"], keep_id
    )

    # Delete merge project
    await app.db.execute("DELETE FROM projects WHERE id = $1", merge_id)

    return json.dumps({
        "success": True,
        "message": f"Merged '{merge_row['name']}' into '{keep_row['name']}'",
        "moved": moved,
        "alias_created": merge_row["name"]
    })
```

**Step 2: Commit**

```bash
git add src/tools/admin.py
git commit -m "feat: add merge_projects tool with alias creation"
```

---

### Task 9: Update All Tools to Use Project Resolver

**Files:**
- Modify: `src/tools/lessons.py` (log_lesson)
- Modify: `src/tools/sessions.py` (start_session)
- Modify: `src/tools/journal.py` (write_journal, read_journal)
- Modify: `src/tools/admin.py` (update_project_state, check_guardrails)
- Modify: `src/tools/projects.py` (get_project)
- Modify: `src/tools/infra.py` (get_connectivity)

**Step 1: Replace all direct project name lookups**

In every tool that does:
```python
row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
```

Replace with:
```python
from src.helpers import resolve_project_id
project_id = await resolve_project_id(app.db, project)
```

This gives case-insensitive matching with alias support everywhere.

**Step 2: Commit**

```bash
git add src/tools/
git commit -m "refactor: use resolve_project_id across all tools for consistent name resolution"
```

---

### Task 10: Apply Migration and Deploy

**Step 1: Apply migration to live database**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 \
  "docker exec -i claude_memory_db psql -U claude -d claude_memory" < migrations/002_v2_features.sql
```

**Step 2: Deploy new code**

```bash
./deploy.sh
```

**Step 3: Verify existing tools still work**

From Claude Code, test a few core operations:
- `search("flutter")` - should return results
- `list_projects()` - should list all 21 projects
- `get_project("wine.dine Pro")` - should return full context
- `write_journal(content="v2 deployment test", tags=["test"])` - should succeed

**Step 4: Verify new tools work**

- `set_project_claude_md(project="claude-memory", content="# Claude Memory\n\n## Purpose\nPersistent memory system for Claude sessions.")` - should succeed
- `get_project_claude_md(project="claude-memory")` - should return the content
- `retire_lesson(lesson_id=..., reason="test")` - test with a non-critical lesson, then un-retire by direct SQL if needed

**Step 5: Commit any final fixes and tag**

```bash
git tag v2.0.0
git push origin main --tags
```

---

### Task 11: Data Cleanup

**Step 1: Merge duplicate projects**

```
merge_projects(keep="dungeondays", merge="Dungeon Days")
```

Verify: `get_project("Dungeon Days")` should now resolve via alias to dungeondays.

**Step 2: Seed common aliases for existing projects**

Run via the `merge_projects` tool or direct SQL for projects that don't have duplicates but might get called by variations:

```sql
INSERT INTO project_aliases (alias, project_id)
SELECT 'recipe sync', id FROM projects WHERE name = 'recipe.sync'
ON CONFLICT DO NOTHING;

INSERT INTO project_aliases (alias, project_id)
SELECT 'recipesync', id FROM projects WHERE name = 'recipe.sync'
ON CONFLICT DO NOTHING;

INSERT INTO project_aliases (alias, project_id)
SELECT 'wine dine pro', id FROM projects WHERE name = 'wine.dine Pro'
ON CONFLICT DO NOTHING;

INSERT INTO project_aliases (alias, project_id)
SELECT 'winedine', id FROM projects WHERE name = 'wine.dine'
ON CONFLICT DO NOTHING;
```
