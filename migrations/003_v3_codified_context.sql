-- migrations/003_v3_codified_context.sql
-- v3: Codified Context Infrastructure
-- Adds: agent_specs, specifications, mcp_servers, mcp_server_tools, mcp_server_projects
-- Updates: semantic_search function to include new entity types

-- =============================================================================
-- Agent Specifications
-- =============================================================================

CREATE TABLE agent_specs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    spec_content TEXT NOT NULL,
    summary TEXT,
    model VARCHAR(20) DEFAULT 'sonnet',
    triggers TEXT[] DEFAULT '{}',
    tools TEXT[] DEFAULT '{}',
    project_id INT REFERENCES projects(id) ON DELETE SET NULL,
    version INT DEFAULT 1,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    retired_at TIMESTAMP,
    retired_reason TEXT,
    embedding VECTOR(1536)
);

CREATE INDEX idx_agent_specs_project ON agent_specs(project_id);
CREATE INDEX idx_agent_specs_triggers ON agent_specs USING GIN(triggers);
CREATE INDEX idx_agent_specs_retired ON agent_specs(retired_at) WHERE retired_at IS NULL;
CREATE INDEX idx_agent_specs_embedding ON agent_specs
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- =============================================================================
-- Specification Documents
-- =============================================================================

CREATE TABLE specifications (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    subsystem VARCHAR(100),
    content TEXT NOT NULL,
    summary TEXT,
    format_hints TEXT[] DEFAULT '{}',
    triggers TEXT[] DEFAULT '{}',
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INT DEFAULT 1,
    verified_at TIMESTAMP DEFAULT NOW(),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    retired_at TIMESTAMP,
    retired_reason TEXT,
    embedding VECTOR(1536)
);

CREATE INDEX idx_specs_project ON specifications(project_id);
CREATE INDEX idx_specs_subsystem ON specifications(subsystem);
CREATE INDEX idx_specs_triggers ON specifications USING GIN(triggers);
CREATE INDEX idx_specs_retired ON specifications(retired_at) WHERE retired_at IS NULL;
CREATE INDEX idx_specs_embedding ON specifications
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- =============================================================================
-- MCP Server Registry
-- =============================================================================

CREATE TABLE mcp_servers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    url TEXT,
    transport VARCHAR(20) NOT NULL,
    machine_id INT REFERENCES machines(id) ON DELETE SET NULL,
    auth_type VARCHAR(20) DEFAULT 'none',
    auth_hint TEXT,
    config_snippet JSONB,
    limitations TEXT,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    retired_at TIMESTAMP,
    retired_reason TEXT,
    embedding VECTOR(1536)
);

CREATE INDEX idx_mcp_servers_machine ON mcp_servers(machine_id);
CREATE INDEX idx_mcp_servers_status ON mcp_servers(status);
CREATE INDEX idx_mcp_servers_retired ON mcp_servers(retired_at) WHERE retired_at IS NULL;
CREATE INDEX idx_mcp_servers_embedding ON mcp_servers
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE mcp_server_tools (
    id SERIAL PRIMARY KEY,
    server_id INT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    tool_name VARCHAR(100) NOT NULL,
    description TEXT NOT NULL,
    parameters JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    embedding VECTOR(1536),
    UNIQUE(server_id, tool_name)
);

CREATE INDEX idx_mcp_tools_server ON mcp_server_tools(server_id);
CREATE INDEX idx_mcp_tools_embedding ON mcp_server_tools
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE mcp_server_projects (
    server_id INT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (server_id, project_id)
);

-- =============================================================================
-- Updated Semantic Search Function
-- =============================================================================

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

        UNION ALL

        SELECT
            'agent_spec'::TEXT as source_type,
            a.id as source_id,
            a.name::TEXT as title,
            COALESCE(a.summary, a.description)::TEXT as content,
            1 - (a.embedding <=> query_embedding) as similarity
        FROM agent_specs a
        WHERE a.embedding IS NOT NULL AND a.retired_at IS NULL

        UNION ALL

        SELECT
            'specification'::TEXT as source_type,
            sp.id as source_id,
            sp.title::TEXT as title,
            COALESCE(sp.summary, sp.title)::TEXT as content,
            1 - (sp.embedding <=> query_embedding) as similarity
        FROM specifications sp
        WHERE sp.embedding IS NOT NULL AND sp.retired_at IS NULL

        UNION ALL

        SELECT
            'mcp_tool'::TEXT as source_type,
            mt.id as source_id,
            mt.tool_name::TEXT as title,
            mt.description::TEXT as content,
            1 - (mt.embedding <=> query_embedding) as similarity
        FROM mcp_server_tools mt
        WHERE mt.embedding IS NOT NULL
    ) combined
    ORDER BY similarity DESC
    LIMIT search_limit;
END;
$$ LANGUAGE plpgsql;
