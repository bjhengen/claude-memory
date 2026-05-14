-- v6: Multi-agent attribution
-- Adds source_agent + source_client_id columns to writeable tables.
-- Creates api_keys (static bearer tokens) and oauth_client_family.
-- Backfills existing rows to source_agent='claude'.
-- Adds left/right_source_agent + generated cross_agent column to backlog_analysis.

BEGIN;

-- ============================================
-- New tables
-- ============================================

CREATE TABLE IF NOT EXISTS api_keys (
    id              SERIAL PRIMARY KEY,
    api_key_hash    TEXT NOT NULL UNIQUE,
    family          TEXT NOT NULL,
    client_name     TEXT,
    label           TEXT,
    scopes          TEXT[] NOT NULL DEFAULT ARRAY['read','write'],
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMP,
    revoked_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_api_keys_active
    ON api_keys (revoked_at) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS oauth_client_family (
    client_id       TEXT PRIMARY KEY REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    family          TEXT NOT NULL,
    client_name     TEXT,
    inferred_from   TEXT NOT NULL,
    inferred_at     TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ============================================
-- Owned content: source_agent + source_client_id + audit fields
-- ============================================

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['lessons','patterns','journal','agent_specs',
                             'specifications','mcp_servers','mcp_server_tools',
                             'annotations']
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT ''claude''', t);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_client_id TEXT', t);
    END LOOP;
END $$;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['lessons','agent_specs','specifications','mcp_servers',
                             'patterns','annotations','journal']
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS retired_by_agent TEXT', t);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS updated_by_agent TEXT', t);
    END LOOP;
END $$;

-- ============================================
-- Shared metadata: source_agent + source_client_id
-- ============================================

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['projects','project_state','approaches','key_files',
                             'guardrails','permissions','project_aliases','machines',
                             'databases','containers','sessions']
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_agent     TEXT NOT NULL DEFAULT ''claude''', t);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS source_client_id TEXT', t);
    END LOOP;
END $$;

-- conflicts table (may not exist in all environments — schema is v5+ only)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'conflicts') THEN
        EXECUTE 'ALTER TABLE conflicts ADD COLUMN IF NOT EXISTS source_agent TEXT NOT NULL DEFAULT ''claude''';
        EXECUTE 'ALTER TABLE conflicts ADD COLUMN IF NOT EXISTS source_client_id TEXT';
    END IF;
END $$;

-- Intentionally NOT attributing mcp_server_projects (composite PK, junction).

-- ============================================
-- Backlog audit attribution
-- ============================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'consolidation_runs') THEN
        EXECUTE 'ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS source_agent TEXT';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backlog_analysis') THEN
        EXECUTE 'ALTER TABLE backlog_analysis ADD COLUMN IF NOT EXISTS left_source_agent  TEXT';
        EXECUTE 'ALTER TABLE backlog_analysis ADD COLUMN IF NOT EXISTS right_source_agent TEXT';
        EXECUTE 'UPDATE backlog_analysis ba
                 SET left_source_agent  = la.source_agent,
                     right_source_agent = lb.source_agent
                 FROM lessons la, lessons lb
                 WHERE ba.lesson_a_id = la.id
                   AND ba.lesson_b_id = lb.id
                   AND ba.left_source_agent IS NULL';
        EXECUTE 'ALTER TABLE backlog_analysis
                 ADD COLUMN IF NOT EXISTS cross_agent BOOLEAN
                 GENERATED ALWAYS AS (left_source_agent IS DISTINCT FROM right_source_agent) STORED';
    END IF;
END $$;

COMMIT;
