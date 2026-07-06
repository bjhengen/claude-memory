-- =============================================================================
-- Migration: v8_client_last_seen.sql
-- Date: 2026-07-06
-- Purpose: Per-client last-seen visibility across BOTH auth paths (review P1.5).
--   api_keys.last_seen_at existed since v6; OAuth clients had no equivalent,
--   so a launch context going dark (e.g. the work-laptop CLI registration
--   vanishing May-Jul 2026, lessons #1380/#1381) was undetectable server-side.
--   resolve_identity now stamps oauth_clients.last_seen_at, and the new
--   non-admin get_client_health tool surfaces staleness per client.
-- Idempotent: safe to run multiple times.
-- =============================================================================

ALTER TABLE oauth_clients ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP;
