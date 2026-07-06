-- =============================================================================
-- Migration: v9_recency_ranking.sql
-- Date: 2026-07-06
-- Purpose: Recency-aware, confidence-free ranking (review P2.1 + P2.2).
--   1. memory_rank(): the ONE scoring definition for ranked retrieval —
--      score = (similarity + keyword_boost) * recency_factor, where
--      recency_factor = 0.7 + 0.3 * exp(-age_days / 180), bounded [0.7, 1.0]
--      so semantic relevance still dominates: age can cost at most 30%.
--      NULL age (unknown) is treated as 365 days.
--   2. The v4 vote-confidence multiplier is REMOVED from ranking: unrated
--      lessons already scored 1.0 (the cap), so it could never promote and
--      demoted almost nothing — zero rated lessons in 14 months of use.
--      Vote counts are still returned for display and canonical selection.
--   3. semantic_search() gains filter_source_agent, pushed into SQL —
--      replaces the lossy oversample-then-post-filter in the search tool.
-- Idempotent: safe to run multiple times.
-- =============================================================================

CREATE OR REPLACE FUNCTION memory_rank(
    similarity FLOAT,
    keyword_boost FLOAT,
    age_days FLOAT
) RETURNS FLOAT AS $$
    SELECT (similarity + keyword_boost)
           * (0.7 + 0.3 * exp(-GREATEST(COALESCE(age_days, 365.0), 0.0) / 180.0))
$$ LANGUAGE SQL IMMUTABLE;

-- Signature changes (new parameter), so drop the old function first.
DROP FUNCTION IF EXISTS semantic_search(VECTOR(1536), TEXT, INT);

CREATE FUNCTION semantic_search(
    query_embedding VECTOR(1536),
    query_text TEXT,
    search_limit INT DEFAULT 5,
    filter_source_agent TEXT DEFAULT NULL
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
        -- Lessons
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
            memory_rank(
                (1 - (l.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN l.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(l.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - l.learned_at)) / 86400.0)::FLOAT
            )::FLOAT AS effective_score,
            l.upvotes AS upvotes,
            l.downvotes AS downvotes
        FROM lessons l
        WHERE l.embedding IS NOT NULL AND l.retired_at IS NULL
          AND (filter_source_agent IS NULL OR l.source_agent = filter_source_agent)

        UNION ALL

        -- Patterns
        SELECT
            'pattern'::TEXT, p.id, p.name::TEXT, p.problem::TEXT,
            (1 - (p.embedding <=> query_embedding))::FLOAT,
            (CASE
                WHEN p.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(p.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT,
            memory_rank(
                (1 - (p.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN p.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(p.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - p.created_at)) / 86400.0)::FLOAT
            )::FLOAT,
            0, 0
        FROM patterns p
        WHERE p.embedding IS NOT NULL
          AND (filter_source_agent IS NULL OR p.source_agent = filter_source_agent)

        UNION ALL

        -- Sessions
        SELECT
            'session'::TEXT, s.id, ('Session ' || s.id)::TEXT, s.summary::TEXT,
            (1 - (s.embedding <=> query_embedding))::FLOAT,
            (CASE
                WHEN s.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(s.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT,
            memory_rank(
                (1 - (s.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN s.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(s.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - COALESCE(s.ended_at, s.started_at))) / 86400.0)::FLOAT
            )::FLOAT,
            0, 0
        FROM sessions s
        WHERE s.embedding IS NOT NULL
          AND (filter_source_agent IS NULL OR s.source_agent = filter_source_agent)

        UNION ALL

        -- Journal
        SELECT
            'journal'::TEXT, j.id,
            ('Journal ' || to_char(j.entry_date, 'YYYY-MM-DD'))::TEXT, j.content::TEXT,
            (1 - (j.embedding <=> query_embedding))::FLOAT,
            (CASE
                WHEN j.tsv @@ plainto_tsquery('english', query_text)
                THEN ts_rank(j.tsv, plainto_tsquery('english', query_text)) * 0.3
                ELSE 0.0
            END)::FLOAT,
            memory_rank(
                (1 - (j.embedding <=> query_embedding))::FLOAT,
                (CASE
                    WHEN j.tsv @@ plainto_tsquery('english', query_text)
                    THEN ts_rank(j.tsv, plainto_tsquery('english', query_text)) * 0.3
                    ELSE 0.0
                END)::FLOAT,
                (EXTRACT(EPOCH FROM (NOW() - j.entry_date)) / 86400.0)::FLOAT
            )::FLOAT,
            0, 0
        FROM journal j
        WHERE j.embedding IS NOT NULL
          AND (filter_source_agent IS NULL OR j.source_agent = filter_source_agent)
    ) combined
    ORDER BY effective_score DESC
    LIMIT search_limit;
END;
$$ LANGUAGE plpgsql;
