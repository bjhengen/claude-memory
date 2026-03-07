# V4 Feedback Loop & Search Improvements — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add lesson ratings, polymorphic annotations, and hybrid keyword+semantic search to claude-memory.

**Architecture:** Three independent features that connect at the search layer. Lesson ratings add columns to `lessons` and a new tool. Annotations add a new table and tool module. Hybrid search adds `tsvector` columns and triggers to all searchable tables, modifying the scoring formula in existing search tools. All three converge in the updated `semantic_search` function and `search_lessons`/`find_context` queries.

**Tech Stack:** PostgreSQL 16 (pgvector, tsvector), Python 3.11, FastMCP, asyncpg

**Design doc:** `docs/plans/2026-03-07-v4-feedback-loop-design.md`

---

### Task 1: SQL Migration — Lesson Ratings Columns

**Files:**
- Create: `db/migrations/v4_feedback_loop.sql`

**Step 1: Create migration file with lesson rating columns**

```sql
-- V4: Feedback Loop & Search Improvements
-- Run against production: docker exec -i claude_memory_db psql -U claude -d claude_memory

-- ============================================
-- 1. Lesson Ratings
-- ============================================

ALTER TABLE lessons ADD COLUMN IF NOT EXISTS upvotes INT DEFAULT 0;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS downvotes INT DEFAULT 0;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS last_rated_at TIMESTAMP;
```

**Step 2: Commit**

```bash
git add db/migrations/v4_feedback_loop.sql
git commit -m "feat(db): add lesson rating columns migration (v4 part 1)"
```

---

### Task 2: SQL Migration — Annotations Table

**Files:**
- Modify: `db/migrations/v4_feedback_loop.sql`

**Step 1: Append annotations table to migration**

```sql
-- ============================================
-- 2. Annotations (polymorphic)
-- ============================================

CREATE TABLE IF NOT EXISTS annotations (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id INT NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_annotations_entity ON annotations(entity_type, entity_id);
```

**Step 2: Commit**

```bash
git add db/migrations/v4_feedback_loop.sql
git commit -m "feat(db): add annotations table migration (v4 part 2)"
```

---

### Task 3: SQL Migration — tsvector Columns, Triggers, and Backfill

**Files:**
- Modify: `db/migrations/v4_feedback_loop.sql`

**Step 1: Append tsvector columns and GIN indexes**

```sql
-- ============================================
-- 3. Hybrid Search — tsvector columns
-- ============================================

-- Core tables
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE patterns ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE journal ADD COLUMN IF NOT EXISTS tsv tsvector;

-- V3 tables
ALTER TABLE agent_specs ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE specifications ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE mcp_server_tools ADD COLUMN IF NOT EXISTS tsv tsvector;

-- GIN indexes
CREATE INDEX IF NOT EXISTS idx_lessons_tsv ON lessons USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_patterns_tsv ON patterns USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_sessions_tsv ON sessions USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_journal_tsv ON journal USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_agent_specs_tsv ON agent_specs USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_specifications_tsv ON specifications USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_mcp_server_tools_tsv ON mcp_server_tools USING gin(tsv);
```

**Step 2: Append triggers for auto-populating tsvector on insert/update**

```sql
-- ============================================
-- 4. tsvector triggers
-- ============================================

-- Lessons: title (A), content (B)
CREATE OR REPLACE FUNCTION lessons_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
               setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lessons_tsv_update ON lessons;
CREATE TRIGGER lessons_tsv_update
    BEFORE INSERT OR UPDATE OF title, content ON lessons
    FOR EACH ROW EXECUTE FUNCTION lessons_tsv_trigger();

-- Patterns: name (A), problem + solution (B)
CREATE OR REPLACE FUNCTION patterns_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
               setweight(to_tsvector('english', COALESCE(NEW.problem, '') || ' ' || COALESCE(NEW.solution, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS patterns_tsv_update ON patterns;
CREATE TRIGGER patterns_tsv_update
    BEFORE INSERT OR UPDATE OF name, problem, solution ON patterns
    FOR EACH ROW EXECUTE FUNCTION patterns_tsv_trigger();

-- Sessions: summary (B only, no title)
CREATE OR REPLACE FUNCTION sessions_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english', COALESCE(NEW.summary, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sessions_tsv_update ON sessions;
CREATE TRIGGER sessions_tsv_update
    BEFORE INSERT OR UPDATE OF summary ON sessions
    FOR EACH ROW EXECUTE FUNCTION sessions_tsv_trigger();

-- Journal: content (B only)
CREATE OR REPLACE FUNCTION journal_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english', COALESCE(NEW.content, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS journal_tsv_update ON journal;
CREATE TRIGGER journal_tsv_update
    BEFORE INSERT OR UPDATE OF content ON journal
    FOR EACH ROW EXECUTE FUNCTION journal_tsv_trigger();

-- Agent specs: name (A), description + summary (B)
CREATE OR REPLACE FUNCTION agent_specs_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
               setweight(to_tsvector('english', COALESCE(NEW.description, '') || ' ' || COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_specs_tsv_update ON agent_specs;
CREATE TRIGGER agent_specs_tsv_update
    BEFORE INSERT OR UPDATE OF name, description, summary ON agent_specs
    FOR EACH ROW EXECUTE FUNCTION agent_specs_tsv_trigger();

-- Specifications: title (A), summary (B)
CREATE OR REPLACE FUNCTION specifications_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
               setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS specifications_tsv_update ON specifications;
CREATE TRIGGER specifications_tsv_update
    BEFORE INSERT OR UPDATE OF title, summary ON specifications
    FOR EACH ROW EXECUTE FUNCTION specifications_tsv_trigger();

-- MCP server tools: tool_name (A), description (B)
CREATE OR REPLACE FUNCTION mcp_tools_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := setweight(to_tsvector('english', COALESCE(NEW.tool_name, '')), 'A') ||
               setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mcp_tools_tsv_update ON mcp_server_tools;
CREATE TRIGGER mcp_tools_tsv_update
    BEFORE INSERT OR UPDATE OF tool_name, description ON mcp_server_tools
    FOR EACH ROW EXECUTE FUNCTION mcp_tools_tsv_trigger();
```

**Step 3: Append backfill for existing rows**

```sql
-- ============================================
-- 5. Backfill tsvector for existing data
-- ============================================

UPDATE lessons SET tsv = setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
                         setweight(to_tsvector('english', COALESCE(content, '')), 'B')
WHERE tsv IS NULL;

UPDATE patterns SET tsv = setweight(to_tsvector('english', COALESCE(name, '')), 'A') ||
                          setweight(to_tsvector('english', COALESCE(problem, '') || ' ' || COALESCE(solution, '')), 'B')
WHERE tsv IS NULL;

UPDATE sessions SET tsv = to_tsvector('english', COALESCE(summary, ''))
WHERE tsv IS NULL;

UPDATE journal SET tsv = to_tsvector('english', COALESCE(content, ''))
WHERE tsv IS NULL;

UPDATE agent_specs SET tsv = setweight(to_tsvector('english', COALESCE(name, '')), 'A') ||
                             setweight(to_tsvector('english', COALESCE(description, '') || ' ' || COALESCE(summary, '')), 'B')
WHERE tsv IS NULL;

UPDATE specifications SET tsv = setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
                                setweight(to_tsvector('english', COALESCE(summary, '')), 'B')
WHERE tsv IS NULL;

UPDATE mcp_server_tools SET tsv = setweight(to_tsvector('english', COALESCE(tool_name, '')), 'A') ||
                                  setweight(to_tsvector('english', COALESCE(description, '')), 'B')
WHERE tsv IS NULL;
```

**Step 4: Commit**

```bash
git add db/migrations/v4_feedback_loop.sql
git commit -m "feat(db): add tsvector columns, triggers, and backfill (v4 part 3)"
```

---

### Task 4: SQL Migration — Update semantic_search Function

**Files:**
- Modify: `db/migrations/v4_feedback_loop.sql`

**Step 1: Append updated semantic_search function**

The function gains a `query_text` parameter and applies keyword boost + lesson confidence scoring.

```sql
-- ============================================
-- 6. Updated semantic_search function
-- ============================================

CREATE OR REPLACE FUNCTION semantic_search(
    query_embedding VECTOR(1536),
    query_text TEXT,
    search_limit INT DEFAULT 5
)
RETURNS TABLE (
    source_type TEXT,
    source_id INT,
    title TEXT,
    content TEXT,
    similarity FLOAT,
    keyword_boost FLOAT,
    effective_score FLOAT,
    upvotes INT,
    downvotes INT
) AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM (
        SELECT
            'lesson'::TEXT as source_type,
            l.id as source_id,
            l.title::TEXT as title,
            l.content::TEXT as content,
            (1 - (l.embedding <=> query_embedding))::FLOAT as similarity,
            (CASE WHEN l.tsv @@ plainto_tsquery('english', query_text)
                  THEN ts_rank(l.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                  ELSE 0.0
            END)::FLOAT as keyword_boost,
            (
                (1 - (l.embedding <=> query_embedding))
                + CASE WHEN l.tsv @@ plainto_tsquery('english', query_text)
                       THEN ts_rank(l.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                       ELSE 0.0
                  END
            )::FLOAT
            * (CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                    THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                    ELSE 1.0
               END)::FLOAT as effective_score,
            COALESCE(l.upvotes, 0) as upvotes,
            COALESCE(l.downvotes, 0) as downvotes
        FROM lessons l
        WHERE l.embedding IS NOT NULL AND l.retired_at IS NULL

        UNION ALL

        SELECT
            'pattern'::TEXT as source_type,
            p.id as source_id,
            p.name::TEXT as title,
            p.problem::TEXT as content,
            (1 - (p.embedding <=> query_embedding))::FLOAT as similarity,
            (CASE WHEN p.tsv @@ plainto_tsquery('english', query_text)
                  THEN ts_rank(p.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                  ELSE 0.0
            END)::FLOAT as keyword_boost,
            (
                (1 - (p.embedding <=> query_embedding))
                + CASE WHEN p.tsv @@ plainto_tsquery('english', query_text)
                       THEN ts_rank(p.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                       ELSE 0.0
                  END
            )::FLOAT as effective_score,
            0 as upvotes,
            0 as downvotes
        FROM patterns p
        WHERE p.embedding IS NOT NULL

        UNION ALL

        SELECT
            'session'::TEXT as source_type,
            s.id as source_id,
            ('Session ' || s.id::TEXT)::TEXT as title,
            s.summary::TEXT as content,
            (1 - (s.embedding <=> query_embedding))::FLOAT as similarity,
            (CASE WHEN s.tsv @@ plainto_tsquery('english', query_text)
                  THEN ts_rank(s.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                  ELSE 0.0
            END)::FLOAT as keyword_boost,
            (
                (1 - (s.embedding <=> query_embedding))
                + CASE WHEN s.tsv @@ plainto_tsquery('english', query_text)
                       THEN ts_rank(s.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                       ELSE 0.0
                  END
            )::FLOAT as effective_score,
            0 as upvotes,
            0 as downvotes
        FROM sessions s
        WHERE s.embedding IS NOT NULL

        UNION ALL

        SELECT
            'journal'::TEXT as source_type,
            j.id as source_id,
            ('Journal ' || to_char(j.entry_date, 'YYYY-MM-DD'))::TEXT as title,
            j.content::TEXT as content,
            (1 - (j.embedding <=> query_embedding))::FLOAT as similarity,
            (CASE WHEN j.tsv @@ plainto_tsquery('english', query_text)
                  THEN ts_rank(j.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                  ELSE 0.0
            END)::FLOAT as keyword_boost,
            (
                (1 - (j.embedding <=> query_embedding))
                + CASE WHEN j.tsv @@ plainto_tsquery('english', query_text)
                       THEN ts_rank(j.tsv, plainto_tsquery('english', query_text))::FLOAT * 0.3
                       ELSE 0.0
                  END
            )::FLOAT as effective_score,
            0 as upvotes,
            0 as downvotes
        FROM journal j
        WHERE j.embedding IS NOT NULL
    ) combined
    ORDER BY effective_score DESC
    LIMIT search_limit;
END;
$$ LANGUAGE plpgsql;
```

**Step 2: Commit**

```bash
git add db/migrations/v4_feedback_loop.sql
git commit -m "feat(db): update semantic_search with keyword boost and confidence (v4 part 4)"
```

---

### Task 5: Annotation Helper in helpers.py

**Files:**
- Modify: `src/helpers.py`

**Step 1: Add `fetch_annotations` helper function**

Add below the existing `resolve_project_id` function:

```python
async def fetch_annotations(pool: asyncpg.Pool, entity_type: str, entity_id: int) -> list[dict]:
    """Fetch all annotations for an entity. Used by get_* tools for auto-injection."""
    rows = await pool.fetch(
        "SELECT id, note, updated_at FROM annotations "
        "WHERE entity_type = $1 AND entity_id = $2 ORDER BY created_at",
        entity_type, entity_id
    )
    return [
        {"id": r["id"], "note": r["note"], "updated_at": r["updated_at"].isoformat()}
        for r in rows
    ]
```

**Step 2: Commit**

```bash
git add src/helpers.py
git commit -m "feat: add fetch_annotations helper for auto-injection"
```

---

### Task 6: Annotation Tools Module

**Files:**
- Create: `src/tools/annotations.py`
- Modify: `src/server.py` (add import)

**Step 1: Create the annotations tool module**

```python
"""Annotation tools: annotate, get_annotations, clear_annotation."""

import json
from mcp.server.fastmcp import Context

from src.server import mcp


@mcp.tool()
async def annotate(
    entity_type: str,
    entity_id: int,
    note: str,
    ctx: Context = None
) -> str:
    """
    Attach a persistent note to any entity. Annotations auto-appear when
    the entity is fetched. Use for cross-session observations about
    specific lessons, specs, agents, projects, or MCP tools.

    Args:
        entity_type: Type of entity: 'lesson', 'spec', 'agent', 'project', 'mcp_server', 'mcp_tool'
        entity_id: ID of the entity
        note: The annotation text
    """
    valid_types = {'lesson', 'spec', 'agent', 'project', 'mcp_server', 'mcp_tool'}
    if entity_type not in valid_types:
        return json.dumps({"error": f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(sorted(valid_types))}"})

    app = ctx.request_context.lifespan_context

    # Check if annotation already exists for this entity
    existing = await app.db.fetchrow(
        "SELECT id, note FROM annotations WHERE entity_type = $1 AND entity_id = $2",
        entity_type, entity_id
    )

    if existing:
        # Append with timestamp separator
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        updated_note = f"{existing['note']}\n\n---\n[{timestamp}] {note}"
        await app.db.execute(
            "UPDATE annotations SET note = $1, updated_at = NOW() WHERE id = $2",
            updated_note, existing["id"]
        )
        return json.dumps({
            "success": True,
            "annotation_id": existing["id"],
            "message": f"Annotation appended to {entity_type} {entity_id}"
        })

    row = await app.db.fetchrow(
        """
        INSERT INTO annotations (entity_type, entity_id, note)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        entity_type, entity_id, note
    )

    return json.dumps({
        "success": True,
        "annotation_id": row["id"],
        "message": f"Annotation created on {entity_type} {entity_id}"
    })


@mcp.tool()
async def get_annotations(
    entity_type: str,
    entity_id: int,
    ctx: Context = None
) -> str:
    """
    Read all annotations for an entity.

    Args:
        entity_type: Type of entity: 'lesson', 'spec', 'agent', 'project', 'mcp_server', 'mcp_tool'
        entity_id: ID of the entity
    """
    app = ctx.request_context.lifespan_context

    rows = await app.db.fetch(
        "SELECT id, note, created_at, updated_at FROM annotations "
        "WHERE entity_type = $1 AND entity_id = $2 ORDER BY created_at",
        entity_type, entity_id
    )

    return json.dumps({
        "entity_type": entity_type,
        "entity_id": entity_id,
        "annotations": [
            {
                "id": r["id"],
                "note": r["note"],
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat()
            }
            for r in rows
        ]
    })


@mcp.tool()
async def clear_annotation(
    annotation_id: int,
    ctx: Context = None
) -> str:
    """
    Remove an annotation.

    Args:
        annotation_id: ID of the annotation to remove
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, entity_type, entity_id FROM annotations WHERE id = $1",
        annotation_id
    )
    if not existing:
        return json.dumps({"error": f"Annotation {annotation_id} not found"})

    await app.db.execute("DELETE FROM annotations WHERE id = $1", annotation_id)

    return json.dumps({
        "success": True,
        "message": f"Annotation {annotation_id} removed from {existing['entity_type']} {existing['entity_id']}"
    })
```

**Step 2: Register the module in `src/server.py`**

Add this line after the existing tool imports (after line 171):

```python
import src.tools.annotations  # noqa: E402, F401
```

**Step 3: Commit**

```bash
git add src/tools/annotations.py src/server.py
git commit -m "feat: add annotation tools (annotate, get_annotations, clear_annotation)"
```

---

### Task 7: rate_lesson Tool

**Files:**
- Modify: `src/tools/lessons.py`

**Step 1: Add `rate_lesson` function after `retire_lesson`**

```python
@mcp.tool()
async def rate_lesson(
    lesson_id: int,
    rating: str,
    comment: str = None,
    ctx: Context = None
) -> str:
    """
    Rate a lesson as helpful or unhelpful. Ratings affect search ranking —
    low-rated lessons appear lower in results but are never auto-retired.

    Args:
        lesson_id: ID of the lesson to rate
        rating: 'up' or 'down'
        comment: Optional context for the rating (saved as annotation)
    """
    if rating not in ("up", "down"):
        return json.dumps({"error": "rating must be 'up' or 'down'"})

    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, title, upvotes, downvotes FROM lessons WHERE id = $1",
        lesson_id
    )
    if not existing:
        return json.dumps({"error": f"Lesson {lesson_id} not found"})

    column = "upvotes" if rating == "up" else "downvotes"
    await app.db.execute(
        f"UPDATE lessons SET {column} = COALESCE({column}, 0) + 1, last_rated_at = NOW() WHERE id = $1",
        lesson_id
    )

    new_up = (existing["upvotes"] or 0) + (1 if rating == "up" else 0)
    new_down = (existing["downvotes"] or 0) + (1 if rating == "down" else 0)

    # If comment provided, create an annotation
    if comment:
        prefix = "upvote" if rating == "up" else "downvote"
        await app.db.execute(
            """
            INSERT INTO annotations (entity_type, entity_id, note)
            VALUES ('lesson', $1, $2)
            """,
            lesson_id, f"[{prefix}] {comment}"
        )

    return json.dumps({
        "success": True,
        "lesson_id": lesson_id,
        "title": existing["title"],
        "upvotes": new_up,
        "downvotes": new_down,
        "message": f"Lesson {lesson_id} rated '{rating}'"
    })
```

**Step 2: Commit**

```bash
git add src/tools/lessons.py
git commit -m "feat: add rate_lesson tool with annotation integration"
```

---

### Task 8: Update search Tool for Hybrid Search

**Files:**
- Modify: `src/tools/search.py`

**Step 1: Update the `search` function**

The `search` tool needs to pass `query` as raw text alongside the embedding, and handle the new return columns from `semantic_search`.

Replace the existing `search` function:

```python
@mcp.tool()
async def search(query: str, limit: int = 5, ctx: Context = None) -> str:
    """
    Semantic search across lessons, patterns, and session history.
    Returns the most relevant matches based on meaning, not just keywords.

    Args:
        query: What you're looking for (natural language)
        limit: Maximum number of results (default 5)
    """
    app = ctx.request_context.lifespan_context

    # Generate embedding for query
    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    # Search using the semantic_search function (v4: now takes query_text too)
    rows = await app.db.fetch(
        "SELECT * FROM semantic_search($1::vector, $2, $3)",
        embedding_str, query, limit
    )

    if not rows:
        return json.dumps({"results": [], "message": "No matches found"})

    results = []
    for row in rows:
        result = {
            "type": row["source_type"],
            "id": row["source_id"],
            "title": row["title"],
            "content": row["content"][:500] if row["content"] else None,
            "similarity": round(row["similarity"], 3),
            "effective_score": round(row["effective_score"], 3),
        }
        if row["source_type"] == "lesson":
            result["upvotes"] = row["upvotes"]
            result["downvotes"] = row["downvotes"]
        results.append(result)

    return json.dumps({"results": results})
```

**Step 2: Commit**

```bash
git add src/tools/search.py
git commit -m "feat: update search tool for hybrid scoring and rating display"
```

---

### Task 9: Update search_lessons for Hybrid Search + Ratings

**Files:**
- Modify: `src/tools/search.py`

**Step 1: Update `search_lessons` to include keyword boost, confidence, and annotation count**

Replace the existing `search_lessons` function:

```python
@mcp.tool()
async def search_lessons(
    query: str = None,
    project: str = None,
    tags: list[str] = None,
    severity: str = None,
    limit: int = 10,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    Search lessons with optional filters.

    Args:
        query: Semantic search query (optional)
        project: Filter by project name (optional)
        tags: Filter by tags (optional)
        severity: Filter by severity: critical, important, tip (optional)
        limit: Maximum results
        include_retired: Include retired lessons (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []

    if query:
        embedding = await get_embedding(app.openai, query)
        embedding_str = format_embedding(embedding)
        params.append(embedding_str)
        params.append(query)  # for keyword boost
        param_idx = 3
    else:
        param_idx = 1

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            conditions.append(f"p.name IS NOT NULL AND l.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if severity:
        conditions.append(f"l.severity = ${param_idx}")
        params.append(severity)
        param_idx += 1

    if tags:
        conditions.append(f"l.tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    if not include_retired:
        conditions.append("l.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    if query:
        sql = f"""
            SELECT l.*, p.name as project_name,
                   1 - (l.embedding <=> $1::vector) as similarity,
                   CASE WHEN l.tsv @@ plainto_tsquery('english', $2)
                        THEN ts_rank(l.tsv, plainto_tsquery('english', $2)) * 0.3
                        ELSE 0.0
                   END as keyword_boost,
                   CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                        THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                        ELSE 1.0
                   END as confidence,
                   (SELECT COUNT(*) FROM annotations a WHERE a.entity_type = 'lesson' AND a.entity_id = l.id) as annotation_count
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {where_clause} AND l.embedding IS NOT NULL
            ORDER BY (
                (1 - (l.embedding <=> $1::vector))
                + CASE WHEN l.tsv @@ plainto_tsquery('english', $2)
                       THEN ts_rank(l.tsv, plainto_tsquery('english', $2)) * 0.3
                       ELSE 0.0
                  END
            ) * CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                     THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                     ELSE 1.0
                END DESC
            LIMIT ${param_idx}
        """
        params.append(limit)
    else:
        sql = f"""
            SELECT l.*, p.name as project_name,
                   (SELECT COUNT(*) FROM annotations a WHERE a.entity_type = 'lesson' AND a.entity_id = l.id) as annotation_count
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {where_clause}
            ORDER BY l.learned_at DESC
            LIMIT ${param_idx}
        """
        params.append(limit)

    rows = await app.db.fetch(sql, *params)

    results = []
    for row in rows:
        result = {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "project": row.get("project_name"),
            "tags": row["tags"],
            "severity": row["severity"],
            "upvotes": row.get("upvotes", 0) or 0,
            "downvotes": row.get("downvotes", 0) or 0,
            "annotation_count": row.get("annotation_count", 0),
            "learned_at": row["learned_at"].isoformat() if row["learned_at"] else None,
            "similarity": round(row["similarity"], 3) if "similarity" in row.keys() else None,
        }
        results.append(result)

    return json.dumps({"lessons": results})
```

**Step 2: Commit**

```bash
git add src/tools/search.py
git commit -m "feat: update search_lessons with hybrid scoring, confidence, and annotations"
```

---

### Task 10: Update find_context Lessons Tier for Hybrid Search

**Files:**
- Modify: `src/tools/search.py`

**Step 1: Update the lessons tier in `find_context`**

In the `find_context` function, replace the `# --- Lessons tier ---` section (approximately lines 268-306) with keyword boost and confidence scoring. The embedding is already generated as `embedding_str` and the raw query is available as `query`.

Replace the lessons tier block:

```python
    # --- Lessons tier ---
    if "lessons" in active_tiers:
        lesson_conditions = ["l.embedding IS NOT NULL", "l.retired_at IS NULL"]
        lesson_params = [embedding_str, query]  # $1 = embedding, $2 = query text
        lesson_idx = 3

        if project_id:
            lesson_conditions.append(f"l.project_id = ${lesson_idx}")
            lesson_params.append(project_id)
            lesson_idx += 1

        lesson_where = " AND ".join(lesson_conditions)
        lesson_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT l.id, l.title, l.content, l.tags, l.severity,
                   p.name as project_name,
                   1 - (l.embedding <=> $1::vector) as similarity,
                   COALESCE(l.upvotes, 0) as upvotes,
                   COALESCE(l.downvotes, 0) as downvotes,
                   (SELECT COUNT(*) FROM annotations a WHERE a.entity_type = 'lesson' AND a.entity_id = l.id) as annotation_count
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {lesson_where}
            ORDER BY (
                (1 - (l.embedding <=> $1::vector))
                + CASE WHEN l.tsv @@ plainto_tsquery('english', $2)
                       THEN ts_rank(l.tsv, plainto_tsquery('english', $2)) * 0.3
                       ELSE 0.0
                  END
            ) * CASE WHEN (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0)) > 0
                     THEN 0.5 + (COALESCE(l.upvotes, 0)::FLOAT / (COALESCE(l.upvotes, 0) + COALESCE(l.downvotes, 0))::FLOAT * 0.5)
                     ELSE 1.0
                END DESC
            LIMIT ${lesson_idx}
            """,
            *lesson_params
        )

        result["lessons"] = [
            {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"][:300] if row["content"] else None,
                "tags": row["tags"],
                "severity": row["severity"],
                "project": row["project_name"],
                "similarity": round(row["similarity"], 3),
                "upvotes": row["upvotes"],
                "downvotes": row["downvotes"],
                "annotation_count": row["annotation_count"]
            }
            for row in rows
        ]
```

**Step 2: Commit**

```bash
git add src/tools/search.py
git commit -m "feat: update find_context lessons tier with hybrid scoring"
```

---

### Task 11: Auto-Inject Annotations into get_spec and get_agent

**Files:**
- Modify: `src/tools/specs.py`
- Modify: `src/tools/agents.py`

**Step 1: Update `get_spec` to include annotations**

Add import at top of `src/tools/specs.py`:

```python
from src.helpers import resolve_project_id, fetch_annotations
```

In the `get_spec` function, after fetching the row and before the return statement, add:

```python
    annotations = await fetch_annotations(app.db, "spec", spec_id)
```

Add `"annotations": annotations` to the returned JSON dict.

**Step 2: Update `get_agent` to include annotations**

Add import at top of `src/tools/agents.py`:

```python
from src.helpers import resolve_project_id, fetch_annotations
```

In the `get_agent` function, the agent is looked up by name, so after getting the row, fetch annotations using `row["id"]`:

```python
    annotations = await fetch_annotations(app.db, "agent", row["id"])
```

Add `"annotations": annotations` to the returned JSON dict.

**Step 3: Commit**

```bash
git add src/tools/specs.py src/tools/agents.py
git commit -m "feat: auto-inject annotations into get_spec and get_agent"
```

---

### Task 12: Deploy to Production

**Files:** None (remote operations)

**Step 1: Upload changed files to EC2**

```bash
scp -i ~/.ssh/AWS_FR.pem src/tools/annotations.py ubuntu@44.212.169.119:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/tools/lessons.py ubuntu@44.212.169.119:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/tools/search.py ubuntu@44.212.169.119:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/tools/specs.py ubuntu@44.212.169.119:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/tools/agents.py ubuntu@44.212.169.119:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/server.py ubuntu@44.212.169.119:~/claude-memory/src/
scp -i ~/.ssh/AWS_FR.pem src/helpers.py ubuntu@44.212.169.119:~/claude-memory/src/
scp -i ~/.ssh/AWS_FR.pem db/migrations/v4_feedback_loop.sql ubuntu@44.212.169.119:~/claude-memory/db/migrations/
```

**Step 2: Run the migration**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 \
  "docker exec -i claude_memory_db psql -U claude -d claude_memory < ~/claude-memory/db/migrations/v4_feedback_loop.sql"
```

**Step 3: Rebuild and restart the MCP container**

Use the docker-compose v1 workaround (lesson #339):

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "cd ~/claude-memory && docker-compose build mcp"
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker stop claude_memory_mcp && docker rm claude_memory_mcp"
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "cd ~/claude-memory && source .env && docker run -d \
  --name claude_memory_mcp \
  --restart unless-stopped \
  --network claude-memory_claude_memory_net \
  -p 127.0.0.1:8004:8003 \
  -e DATABASE_URL=postgresql://claude:\${POSTGRES_PASSWORD}@db:5432/claude_memory \
  -e OPENAI_API_KEY=\${OPENAI_API_KEY} \
  -e CLAUDE_MEMORY_API_KEY=\${CLAUDE_MEMORY_API_KEY} \
  claude-memory_mcp"
```

**Step 4: Verify health**

```bash
curl -s https://memory.friendly-robots.com/health
```

Expected: `{"status": "healthy", "service": "claude-memory"}`

**Step 5: Verify new tools are registered**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 \
  "docker exec claude_memory_mcp python3 -c \"from src.server import mcp; print([t for t in mcp._tool_manager._tools.keys() if 'annot' in t or 'rate' in t])\""
```

Expected: `['annotate', 'get_annotations', 'clear_annotation', 'rate_lesson']`

---

### Task 13: Verify and Register New Tools

**Step 1: Restart Claude Code to pick up new tools**

Restart Claude Code session so the MCP client refreshes its tool list.

**Step 2: Test rate_lesson**

Call `rate_lesson` on any existing lesson with `rating="up"`. Verify response includes upvotes/downvotes counts.

**Step 3: Test annotate**

Call `annotate(entity_type="lesson", entity_id=<id>, note="test annotation")`. Then call `search_lessons` and verify annotation_count shows.

**Step 4: Test hybrid search**

Call `search("docker ContainerConfig")` — verify the lesson about docker-compose v1 ContainerConfig bug (lesson #339) ranks highly due to keyword boost.

**Step 5: Register new tools in MCP catalog**

Use `register_mcp_tool` to catalog the 4 new tools:

```
register_mcp_tool(server="claude-memory", tool_name="rate_lesson", description="Rate a lesson as helpful or unhelpful. Ratings affect search ranking.")
register_mcp_tool(server="claude-memory", tool_name="annotate", description="Attach a persistent note to any entity (lesson, spec, agent, project, mcp_server, mcp_tool).")
register_mcp_tool(server="claude-memory", tool_name="get_annotations", description="Read all annotations for an entity.")
register_mcp_tool(server="claude-memory", tool_name="clear_annotation", description="Remove an annotation by ID.")
```

**Step 6: Commit final state**

```bash
git add -A && git commit -m "feat: v4 feedback loop complete - ratings, annotations, hybrid search"
```
