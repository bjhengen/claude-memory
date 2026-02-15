-- Migration 002: v2 features
-- Applied: 2026-02-15
-- Description: Project CLAUDE.md, lesson lifecycle, project aliases

-- Project CLAUDE.md
ALTER TABLE projects ADD COLUMN IF NOT EXISTS claude_md TEXT;

-- Lesson lifecycle (soft delete)
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS retired_at TIMESTAMP;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS retired_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_lessons_retired ON lessons(retired_at) WHERE retired_at IS NULL;

-- Project name normalization
CREATE TABLE IF NOT EXISTS project_aliases (
    id SERIAL PRIMARY KEY,
    alias VARCHAR(100) UNIQUE NOT NULL,
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_aliases_alias ON project_aliases(LOWER(alias));

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
