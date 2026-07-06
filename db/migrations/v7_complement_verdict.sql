-- =============================================================================
-- Migration: v7_complement_verdict.sql
-- Date: 2026-07-06
-- Purpose: Add 'complement' to the judge verdict taxonomy (review P1.1).
--   With only duplicate|supersedes|contradicts|unrelated, additive pairs had
--   no correct bucket and were funneled into 'supersedes' (~79% false-positive
--   at the 2026-06-24 queue review, lesson #1327). 'complement' is
--   non-actionable: recorded in backlog_analysis for observability, never
--   enqueued (so consolidation_queue's proposed_action CHECK is unchanged).
-- Idempotent: safe to run multiple times.
-- =============================================================================

ALTER TABLE backlog_analysis DROP CONSTRAINT IF EXISTS backlog_analysis_verdict_check;
ALTER TABLE backlog_analysis ADD CONSTRAINT backlog_analysis_verdict_check
    CHECK (verdict IN ('duplicate', 'supersedes', 'complement', 'contradicts', 'unrelated'));
