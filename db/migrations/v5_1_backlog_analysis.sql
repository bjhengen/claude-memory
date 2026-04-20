-- =============================================================================
-- Migration: v5_1_backlog_analysis.sql
-- Date: 2026-04-20
-- Purpose: Add backlog analysis table for one-shot measurement pass
--   - Records verdicts for every live-lesson pair above cosine threshold
--   - Read-only use: no merges, no retirements are driven by this data
--   - Idempotent and resumable via UNIQUE(batch_run_id, a_id, b_id)
-- Idempotent: safe to run multiple times
-- =============================================================================

CREATE TABLE IF NOT EXISTS backlog_analysis (
    id SERIAL PRIMARY KEY,
    batch_run_id VARCHAR(100) NOT NULL,
    lesson_a_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    lesson_b_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    cosine_similarity NUMERIC(4,3) NOT NULL,
    judge_model VARCHAR(50) NOT NULL,
    verdict VARCHAR(20) NOT NULL CHECK (verdict IN ('duplicate','supersedes','contradicts','unrelated')),
    direction VARCHAR(30),  -- only set when verdict='supersedes'
    confidence NUMERIC(3,2) NOT NULL,
    reasoning TEXT NOT NULL,
    judged_at TIMESTAMP DEFAULT NOW(),
    CHECK (lesson_a_id < lesson_b_id),
    UNIQUE(batch_run_id, lesson_a_id, lesson_b_id)
);

CREATE INDEX IF NOT EXISTS idx_backlog_analysis_batch
    ON backlog_analysis(batch_run_id);
CREATE INDEX IF NOT EXISTS idx_backlog_analysis_verdict
    ON backlog_analysis(verdict, confidence DESC);
