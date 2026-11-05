-- =============================================================================
-- Migration: v10_retrieval_log.sql
-- Date: 2026-11-04
-- Purpose: Record which stored memory each read returned, per client, so usage
--   can be measured across every client (including those with no local
--   transcripts). Holds ids only: query text and returned content are never
--   stored. No foreign keys, so a row outlives the lesson or entry it names.
--   items is a JSON array of {"type": "<kind>", "id": <integer>}.
-- Idempotent: safe to run multiple times. Apply BEFORE deploying the code; the
--   code fails open if the table is missing, but those calls go unrecorded.
-- =============================================================================

CREATE TABLE IF NOT EXISTS retrieval_log (
    id BIGSERIAL PRIMARY KEY,
    called_at TIMESTAMP NOT NULL DEFAULT NOW(),
    tool VARCHAR(64) NOT NULL,
    source_agent VARCHAR(64),
    source_client_id VARCHAR(255),
    result_count INTEGER NOT NULL,
    items JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_retrieval_log_called_at ON retrieval_log (called_at);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_client ON retrieval_log (source_client_id, called_at);
