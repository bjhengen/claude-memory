# Claude Memory v4: Feedback Loop & Search Improvements

**Date:** 2026-03-07
**Status:** Approved
**Inspired by:** [context-hub](https://github.com/andrewyng/context-hub) — annotation, feedback, and BM25 search patterns

## Overview

Three features that close the self-improving loop in claude-memory:

1. **Lesson Ratings** — Agents can upvote/downvote lessons, affecting search ranking
2. **Annotations** — Lightweight sticky notes attached to any entity, auto-surfaced on retrieval
3. **Hybrid Search** — Keyword boost on top of semantic search for exact-match accuracy

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Rating effect | Scored ranking (no auto-retire) | Information-preserving; low-confidence lessons rank lower but don't vanish |
| Rating structure | Up/down only, optional comment | Low friction so agents actually use it; comment becomes annotation |
| Annotation model | Polymorphic table (entity_type + entity_id) | One table, one tool, extends to any entity type |
| Search blending | Keyword boost on semantic | Preserves current search quality; exact matches float up without mode switching |
| Rating in search | All surfaces (search, search_lessons, find_context) | Consistent behavior; low-confidence lessons rank lower everywhere |

---

## Feature 1: Lesson Ratings

### Schema

Add columns to existing `lessons` table:

```sql
ALTER TABLE lessons ADD COLUMN upvotes INT DEFAULT 0;
ALTER TABLE lessons ADD COLUMN downvotes INT DEFAULT 0;
ALTER TABLE lessons ADD COLUMN last_rated_at TIMESTAMP;
```

No separate ratings table. Agents are ephemeral — we don't need per-voter tracking or deduplication.

### New Tool

```python
rate_lesson(lesson_id: int, rating: str, comment: str = None) -> str
```

- `rating` must be `"up"` or `"down"`
- Increments the appropriate counter, updates `last_rated_at`
- If `comment` provided, creates an annotation on the lesson (entity_type='lesson')
- Returns current up/down counts

### Confidence Score

```python
confidence = 1.0  # baseline (no votes)
if (upvotes + downvotes) > 0:
    ratio = upvotes / (upvotes + downvotes)
    confidence = 0.5 + (ratio * 0.5)  # range: 0.5 to 1.0
```

- No votes: 1.0 (neutral)
- All upvotes: 1.0
- All downvotes: 0.5 (halves effective similarity)
- Mixed: proportional

### Search Integration

Applied as: `effective_score = similarity * confidence`

Update `semantic_search` function, `search_lessons`, and `find_context` to include confidence weighting in ORDER BY. Surface `upvotes` and `downvotes` counts in results.

---

## Feature 2: Annotations

### Schema

```sql
CREATE TABLE annotations (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id INT NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_annotations_entity ON annotations(entity_type, entity_id);
```

Valid entity types: `lesson`, `spec`, `agent`, `project`, `mcp_server`, `mcp_tool`

No embedding column — annotations are short notes, not independently searchable.

### New Tools

**`annotate`** — Create or append an annotation:
```python
annotate(entity_type: str, entity_id: int, note: str) -> str
```
- If annotation exists for that entity, appends with timestamp separator
- Returns annotation id

**`get_annotations`** — Read annotations:
```python
get_annotations(entity_type: str, entity_id: int) -> str
```
- Returns all annotations for the entity
- Used internally and available as explicit tool

**`clear_annotation`** — Remove an annotation:
```python
clear_annotation(annotation_id: int) -> str
```
- Hard delete (annotations are ephemeral notes)

### Auto-Injection

Modify existing retrieval tools to include annotations in responses:

- `get_spec` — appends `annotations` array
- `get_agent` — appends `annotations` array
- `search_lessons` — includes annotation count per lesson
- `find_context` — includes annotation count per result

Helper function:
```python
async def fetch_annotations(db, entity_type, entity_id):
    rows = await db.fetch(
        "SELECT id, note, updated_at FROM annotations "
        "WHERE entity_type = $1 AND entity_id = $2 ORDER BY created_at",
        entity_type, entity_id
    )
    return [{"id": r["id"], "note": r["note"], "updated_at": r["updated_at"].isoformat()} for r in rows]
```

### rate_lesson Integration

When `rate_lesson(comment=...)` is provided, automatically creates an annotation with `entity_type='lesson'`. Downvote + comment = visible sticky note explaining why.

---

## Feature 3: Hybrid Search (Keyword Boost)

### Schema

Add `tsvector` columns to all searchable tables:

```sql
-- Core tables (participate in semantic_search)
ALTER TABLE lessons ADD COLUMN tsv tsvector;
ALTER TABLE patterns ADD COLUMN tsv tsvector;
ALTER TABLE sessions ADD COLUMN tsv tsvector;
ALTER TABLE journal ADD COLUMN tsv tsvector;

-- V3 tables (participate in find_context)
ALTER TABLE agent_specs ADD COLUMN tsv tsvector;
ALTER TABLE specifications ADD COLUMN tsv tsvector;
ALTER TABLE mcp_server_tools ADD COLUMN tsv tsvector;

-- GIN indexes for fast text search
CREATE INDEX idx_lessons_tsv ON lessons USING gin(tsv);
CREATE INDEX idx_patterns_tsv ON patterns USING gin(tsv);
CREATE INDEX idx_sessions_tsv ON sessions USING gin(tsv);
CREATE INDEX idx_journal_tsv ON journal USING gin(tsv);
CREATE INDEX idx_agent_specs_tsv ON agent_specs USING gin(tsv);
CREATE INDEX idx_specifications_tsv ON specifications USING gin(tsv);
CREATE INDEX idx_mcp_server_tools_tsv ON mcp_server_tools USING gin(tsv);
```

### Triggers (auto-populate on insert/update)

Each table gets a trigger that builds the tsvector from its text fields with weight priority:

```sql
-- Example for lessons (title=A weight, content=B weight)
CREATE OR REPLACE FUNCTION lessons_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
               setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER lessons_tsv_update
    BEFORE INSERT OR UPDATE OF title, content ON lessons
    FOR EACH ROW EXECUTE FUNCTION lessons_tsv_trigger();
```

Field weights per table:

| Table | A (highest) | B |
|-------|-------------|---|
| lessons | title | content |
| patterns | name | problem, solution |
| sessions | (none) | summary |
| journal | (none) | content |
| agent_specs | name | description, summary |
| specifications | title | summary |
| mcp_server_tools | tool_name | description |

### Backfill Migration

One-time update for existing rows (triggers only fire on insert/update):

```sql
UPDATE lessons SET tsv = setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
                         setweight(to_tsvector('english', COALESCE(content, '')), 'B');
-- ... repeat for each table
```

### Scoring Formula

Keyword boost applied when query matches tsvector:

```sql
CASE WHEN tsv @@ plainto_tsquery('english', $query_text)
     THEN ts_rank(tsv, plainto_tsquery('english', $query_text)) * 0.3
     ELSE 0.0
END AS keyword_boost
```

Final ranking formula for lessons (all three features combined):

```
effective_score = (semantic_similarity + keyword_boost) * confidence
```

- `semantic_similarity` = `1 - (embedding <=> query_embedding)` — range 0.0-1.0
- `keyword_boost` = `ts_rank(...) * 0.3` — range 0.0-~0.3, only when keywords match
- `confidence` = lesson rating factor — range 0.5-1.0

For non-lesson tables: `effective_score = semantic_similarity + keyword_boost` (no confidence factor).

### semantic_search Function Update

Add `query_text` parameter for keyword matching:

```sql
CREATE OR REPLACE FUNCTION semantic_search(
    query_embedding VECTOR(1536),
    query_text TEXT,
    search_limit INT DEFAULT 5
)
```

### No Tool Signature Changes

Callers don't change anything. `search("docker ContainerConfig")` works the same — just returns better results when exact keyword matches exist. The raw query text is passed alongside the embedding internally.

---

## New Tool Summary

| Tool | Module | Purpose |
|------|--------|---------|
| `rate_lesson` | `lessons.py` | Up/down vote a lesson |
| `annotate` | new `annotations.py` | Attach a note to any entity |
| `get_annotations` | `annotations.py` | Read annotations for an entity |
| `clear_annotation` | `annotations.py` | Remove an annotation |

Total tool count: 48 (up from 44)

## Modified Tools

| Tool | Change |
|------|--------|
| `search` | Pass raw query text + apply keyword boost + confidence weighting |
| `search_lessons` | Apply keyword boost + confidence weighting, surface vote counts |
| `find_context` | Apply keyword boost + confidence weighting per tier |
| `get_spec` | Auto-inject annotations |
| `get_agent` | Auto-inject annotations |

## Migration

Single SQL migration file handles all schema changes:
- ALTER TABLE for new columns (upvotes, downvotes, last_rated_at, tsv)
- CREATE TABLE for annotations
- CREATE triggers for tsvector population
- UPDATE existing rows to backfill tsvector
- Replace semantic_search function with new signature
