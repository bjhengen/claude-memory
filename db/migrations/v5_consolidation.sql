-- =============================================================================
-- Migration: v5_consolidation.sql
-- Date: 2026-04-20
-- Purpose: Add log-time lesson consolidation infrastructure
--   1. lesson_merges - audit trail for merge/supersede (reversible)
--   2. lesson_conflicts - flagged contradictions awaiting resolution
--   3. consolidation_queue - medium-confidence proposals awaiting human decision
-- Idempotent: safe to run multiple times
-- =============================================================================

-- =============================================================================
-- 1. lesson_merges: audit trail for duplicate-merge and supersede actions
-- =============================================================================

CREATE TABLE IF NOT EXISTS lesson_merges (
    id SERIAL PRIMARY KEY,
    canonical_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    merged_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    action VARCHAR(20) NOT NULL CHECK (action IN ('merged', 'superseded')),
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    auto_decided BOOLEAN NOT NULL,
    decided_by VARCHAR(100),
    transferred_upvotes INT NOT NULL DEFAULT 0,
    transferred_downvotes INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    reversed_at TIMESTAMP,
    reversed_by VARCHAR(100),
    reversed_reason TEXT,
    UNIQUE(canonical_id, merged_id),
    CHECK (auto_decided = true OR decided_by IS NOT NULL),
    CHECK (canonical_id <> merged_id)
);

CREATE INDEX IF NOT EXISTS idx_lesson_merges_canonical
    ON lesson_merges(canonical_id) WHERE reversed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_lesson_merges_merged
    ON lesson_merges(merged_id) WHERE reversed_at IS NULL;

-- =============================================================================
-- 2. lesson_conflicts: flagged contradictions
-- =============================================================================

CREATE TABLE IF NOT EXISTS lesson_conflicts (
    id SERIAL PRIMARY KEY,
    lesson_a_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    lesson_b_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    flagged_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,
    resolved_by VARCHAR(100),
    resolution VARCHAR(20) CHECK (resolution IN ('kept_a', 'kept_b', 'kept_both', 'irrelevant')),
    resolution_note TEXT,
    CHECK (lesson_a_id < lesson_b_id),
    UNIQUE(lesson_a_id, lesson_b_id)
);

CREATE INDEX IF NOT EXISTS idx_lesson_conflicts_unresolved
    ON lesson_conflicts(flagged_at) WHERE resolved_at IS NULL;

-- =============================================================================
-- 3. consolidation_queue: medium-confidence proposals awaiting review
-- =============================================================================

CREATE TABLE IF NOT EXISTS consolidation_queue (
    id SERIAL PRIMARY KEY,
    new_lesson_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    candidate_lesson_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    proposed_action VARCHAR(20) NOT NULL CHECK (proposed_action IN ('merged', 'superseded')),
    proposed_direction VARCHAR(30),
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    enqueued_at TIMESTAMP DEFAULT NOW(),
    decided_at TIMESTAMP,
    decided_by VARCHAR(100),
    decision VARCHAR(20) CHECK (decision IN ('approved', 'rejected')),
    decision_note TEXT,
    CHECK (new_lesson_id <> candidate_lesson_id)
);

CREATE INDEX IF NOT EXISTS idx_consolidation_queue_pending
    ON consolidation_queue(enqueued_at) WHERE decided_at IS NULL;
