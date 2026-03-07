-- =============================================================================
-- Migration: v4_feedback_loop.sql
-- Date: 2026-03-07
-- Purpose: Add feedback loop infrastructure for claude-memory v4
--   1. Lesson rating columns (upvotes/downvotes)
--   2. Annotations table for entity notes
--   3. tsvector columns + GIN indexes on 7 tables for full-text search
--   4. Triggers to auto-populate tsvector columns
--   5. Backfill existing rows
--   6. Updated semantic_search function with keyword boost + vote confidence
-- Idempotent: safe to run multiple times
-- =============================================================================

-- =============================================================================
-- 1. Lesson Rating Columns
-- =============================================================================

ALTER TABLE lessons ADD COLUMN IF NOT EXISTS upvotes INT DEFAULT 0;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS downvotes INT DEFAULT 0;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS last_rated_at TIMESTAMP;

-- =============================================================================
-- 2. Annotations Table
-- =============================================================================

CREATE TABLE IF NOT EXISTS annotations (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id INT NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_annotations_entity ON annotations(entity_type, entity_id);

-- =============================================================================
-- 3. tsvector Columns and GIN Indexes
-- =============================================================================

ALTER TABLE lessons ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE patterns ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE journal ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE agent_specs ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE specifications ADD COLUMN IF NOT EXISTS tsv tsvector;
ALTER TABLE mcp_server_tools ADD COLUMN IF NOT EXISTS tsv tsvector;

CREATE INDEX IF NOT EXISTS idx_lessons_tsv ON lessons USING GIN(tsv);
CREATE INDEX IF NOT EXISTS idx_patterns_tsv ON patterns USING GIN(tsv);
CREATE INDEX IF NOT EXISTS idx_sessions_tsv ON sessions USING GIN(tsv);
CREATE INDEX IF NOT EXISTS idx_journal_tsv ON journal USING GIN(tsv);
CREATE INDEX IF NOT EXISTS idx_agent_specs_tsv ON agent_specs USING GIN(tsv);
CREATE INDEX IF NOT EXISTS idx_specifications_tsv ON specifications USING GIN(tsv);
CREATE INDEX IF NOT EXISTS idx_mcp_server_tools_tsv ON mcp_server_tools USING GIN(tsv);

-- =============================================================================
-- 4. tsvector Triggers
-- =============================================================================

-- --- lessons: A=title, B=content ---
CREATE OR REPLACE FUNCTION lessons_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lessons_tsv ON lessons;
CREATE TRIGGER trg_lessons_tsv
    BEFORE INSERT OR UPDATE ON lessons
    FOR EACH ROW
    EXECUTE FUNCTION lessons_tsv_trigger();

-- --- patterns: A=name, B=problem+solution ---
CREATE OR REPLACE FUNCTION patterns_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.problem, '') || ' ' || COALESCE(NEW.solution, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_patterns_tsv ON patterns;
CREATE TRIGGER trg_patterns_tsv
    BEFORE INSERT OR UPDATE ON patterns
    FOR EACH ROW
    EXECUTE FUNCTION patterns_tsv_trigger();

-- --- sessions: B=summary ---
CREATE OR REPLACE FUNCTION sessions_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sessions_tsv ON sessions;
CREATE TRIGGER trg_sessions_tsv
    BEFORE INSERT OR UPDATE ON sessions
    FOR EACH ROW
    EXECUTE FUNCTION sessions_tsv_trigger();

-- --- journal: B=content ---
CREATE OR REPLACE FUNCTION journal_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_journal_tsv ON journal;
CREATE TRIGGER trg_journal_tsv
    BEFORE INSERT OR UPDATE ON journal
    FOR EACH ROW
    EXECUTE FUNCTION journal_tsv_trigger();

-- --- agent_specs: A=name, B=description+summary ---
CREATE OR REPLACE FUNCTION agent_specs_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.description, '') || ' ' || COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_specs_tsv ON agent_specs;
CREATE TRIGGER trg_agent_specs_tsv
    BEFORE INSERT OR UPDATE ON agent_specs
    FOR EACH ROW
    EXECUTE FUNCTION agent_specs_tsv_trigger();

-- --- specifications: A=title, B=summary ---
CREATE OR REPLACE FUNCTION specifications_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_specifications_tsv ON specifications;
CREATE TRIGGER trg_specifications_tsv
    BEFORE INSERT OR UPDATE ON specifications
    FOR EACH ROW
    EXECUTE FUNCTION specifications_tsv_trigger();

-- --- mcp_server_tools: A=tool_name, B=description ---
CREATE OR REPLACE FUNCTION mcp_server_tools_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.tool_name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_mcp_server_tools_tsv ON mcp_server_tools;
CREATE TRIGGER trg_mcp_server_tools_tsv
    BEFORE INSERT OR UPDATE ON mcp_server_tools
    FOR EACH ROW
    EXECUTE FUNCTION mcp_server_tools_tsv_trigger();

-- =============================================================================
-- 5. Backfill Existing Data
-- =============================================================================

UPDATE lessons SET tsv =
    setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(content, '')), 'B')
WHERE tsv IS NULL;

UPDATE patterns SET tsv =
    setweight(to_tsvector('english', COALESCE(name, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(problem, '') || ' ' || COALESCE(solution, '')), 'B')
WHERE tsv IS NULL;

UPDATE sessions SET tsv =
    setweight(to_tsvector('english', COALESCE(summary, '')), 'B')
WHERE tsv IS NULL;

UPDATE journal SET tsv =
    setweight(to_tsvector('english', COALESCE(content, '')), 'B')
WHERE tsv IS NULL;

UPDATE agent_specs SET tsv =
    setweight(to_tsvector('english', COALESCE(name, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(description, '') || ' ' || COALESCE(summary, '')), 'B')
WHERE tsv IS NULL;

UPDATE specifications SET tsv =
    setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(summary, '')), 'B')
WHERE tsv IS NULL;

UPDATE mcp_server_tools SET tsv =
    setweight(to_tsvector('english', COALESCE(tool_name, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(description, '')), 'B')
WHERE tsv IS NULL;

-- =============================================================================
-- 6. Updated semantic_search Function
-- =============================================================================

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
        -- Lessons: includes vote-based confidence scoring
        SELECT
            'lesson'::TEXT AS source_type,
            l.id AS source_id,
            l.title::TEXT AS title,
            l.content::TEXT AS content,
            (1 - (l.embedding <=> query_embedding))::FLOAT AS similarity,
            (CASE
                WHEN l.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(l.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT AS keyword_boost,
            (
                (1 - (l.embedding <=> query_embedding))
                + (CASE
                    WHEN l.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(l.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)
            ) * (CASE
                WHEN (l.upvotes + l.downvotes) > 0
                THEN 0.5 + ((l.upvotes::FLOAT / (l.upvotes + l.downvotes)::FLOAT) * 0.5)
                ELSE 1.0
            END)::FLOAT AS effective_score,
            l.upvotes AS upvotes,
            l.downvotes AS downvotes
        FROM lessons l
        WHERE l.embedding IS NOT NULL AND l.retired_at IS NULL

        UNION ALL

        -- Patterns
        SELECT
            'pattern'::TEXT AS source_type,
            p.id AS source_id,
            p.name::TEXT AS title,
            p.problem::TEXT AS content,
            (1 - (p.embedding <=> query_embedding))::FLOAT AS similarity,
            (CASE
                WHEN p.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(p.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT AS keyword_boost,
            (
                (1 - (p.embedding <=> query_embedding))
                + (CASE
                    WHEN p.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(p.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)
            )::FLOAT AS effective_score,
            0 AS upvotes,
            0 AS downvotes
        FROM patterns p
        WHERE p.embedding IS NOT NULL

        UNION ALL

        -- Sessions
        SELECT
            'session'::TEXT AS source_type,
            s.id AS source_id,
            ('Session ' || s.id)::TEXT AS title,
            s.summary::TEXT AS content,
            (1 - (s.embedding <=> query_embedding))::FLOAT AS similarity,
            (CASE
                WHEN s.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(s.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT AS keyword_boost,
            (
                (1 - (s.embedding <=> query_embedding))
                + (CASE
                    WHEN s.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(s.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)
            )::FLOAT AS effective_score,
            0 AS upvotes,
            0 AS downvotes
        FROM sessions s
        WHERE s.embedding IS NOT NULL

        UNION ALL

        -- Journal
        SELECT
            'journal'::TEXT AS source_type,
            j.id AS source_id,
            ('Journal ' || to_char(j.entry_date, 'YYYY-MM-DD'))::TEXT AS title,
            j.content::TEXT AS content,
            (1 - (j.embedding <=> query_embedding))::FLOAT AS similarity,
            (CASE
                WHEN j.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(j.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT AS keyword_boost,
            (
                (1 - (j.embedding <=> query_embedding))
                + (CASE
                    WHEN j.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(j.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)
            )::FLOAT AS effective_score,
            0 AS upvotes,
            0 AS downvotes
        FROM journal j
        WHERE j.embedding IS NOT NULL
    ) combined
    ORDER BY effective_score DESC
    LIMIT search_limit;
END;
$$ LANGUAGE plpgsql;
