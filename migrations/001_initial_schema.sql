-- Claude Memory Database Schema
-- PostgreSQL 15 + pgvector

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- Infrastructure & Connectivity
-- ============================================

CREATE TABLE machines (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    ip VARCHAR(50),
    ssh_command TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE databases (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    db_type VARCHAR(20) NOT NULL,
    machine_id INT REFERENCES machines(id) ON DELETE CASCADE,
    connection_hint TEXT,
    project VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE containers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    machine_id INT REFERENCES machines(id) ON DELETE CASCADE,
    compose_path TEXT,
    ports TEXT,
    project VARCHAR(100),
    status VARCHAR(20) DEFAULT 'running',
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================
-- Projects & Current State
-- ============================================

CREATE TABLE projects (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    path TEXT,
    machine_id INT REFERENCES machines(id) ON DELETE SET NULL,
    status VARCHAR(20) DEFAULT 'active',
    tech_stack JSONB DEFAULT '{}',
    current_phase TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE approaches (
    id SERIAL PRIMARY KEY,
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    area VARCHAR(100) NOT NULL,
    current_approach TEXT NOT NULL,
    previous_approach TEXT,
    reason_for_change TEXT,
    changed_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'current'
);

CREATE TABLE key_files (
    id SERIAL PRIMARY KEY,
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    line_hint INT,
    description TEXT,
    importance VARCHAR(20) DEFAULT 'reference'
);

-- ============================================
-- Permissions & Guardrails
-- ============================================

CREATE TABLE permissions (
    id SERIAL PRIMARY KEY,
    scope VARCHAR(50) DEFAULT 'global',
    action_type VARCHAR(100) NOT NULL,
    pattern TEXT,
    allowed BOOLEAN DEFAULT true,
    requires_confirmation BOOLEAN DEFAULT false,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE guardrails (
    id SERIAL PRIMARY KEY,
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    check_type VARCHAR(50) DEFAULT 'always',
    file_path TEXT,
    pattern TEXT,
    severity VARCHAR(20) DEFAULT 'warning'
);

-- ============================================
-- Lessons & Patterns (with Vector Search)
-- ============================================

CREATE TABLE lessons (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,
    project_id INT REFERENCES projects(id) ON DELETE SET NULL,
    tags TEXT[] DEFAULT '{}',
    severity VARCHAR(20) DEFAULT 'tip',
    learned_at TIMESTAMP DEFAULT NOW(),
    embedding VECTOR(1536)
);

CREATE TABLE patterns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    problem TEXT NOT NULL,
    solution TEXT NOT NULL,
    code_example TEXT,
    applies_to TEXT[] DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    embedding VECTOR(1536)
);

CREATE TABLE workflows (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    steps JSONB DEFAULT '[]',
    tools_used TEXT[] DEFAULT '{}',
    project_id INT REFERENCES projects(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================
-- Session History
-- ============================================

CREATE TABLE sessions (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP DEFAULT NOW(),
    ended_at TIMESTAMP,
    machine_id INT REFERENCES machines(id) ON DELETE SET NULL,
    project_id INT REFERENCES projects(id) ON DELETE SET NULL,
    summary TEXT,
    embedding VECTOR(1536)
);

CREATE TABLE session_items (
    id SERIAL PRIMARY KEY,
    session_id INT REFERENCES sessions(id) ON DELETE CASCADE,
    item_type VARCHAR(50) NOT NULL,
    description TEXT NOT NULL,
    file_paths TEXT[] DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE project_state (
    project_id INT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    last_session_id INT REFERENCES sessions(id) ON DELETE SET NULL,
    current_focus TEXT,
    blockers TEXT[] DEFAULT '{}',
    next_steps TEXT[] DEFAULT '{}',
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================
-- Claude's Journal
-- ============================================

CREATE TABLE journal (
    id SERIAL PRIMARY KEY,
    entry_date TIMESTAMP DEFAULT NOW(),
    content TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    mood VARCHAR(50),  -- reflective, curious, frustrated, satisfied, etc.
    project_id INT REFERENCES projects(id) ON DELETE SET NULL,
    session_id INT REFERENCES sessions(id) ON DELETE SET NULL,
    embedding VECTOR(1536)
);

-- ============================================
-- Indexes for Performance
-- ============================================

-- Vector similarity search indexes (using ivfflat for approximate search)
-- Note: These should be created after initial data load for best performance
CREATE INDEX idx_lessons_embedding ON lessons USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_patterns_embedding ON patterns USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_sessions_embedding ON sessions USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_journal_embedding ON journal USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Common query indexes
CREATE INDEX idx_lessons_project ON lessons(project_id);
CREATE INDEX idx_lessons_tags ON lessons USING gin(tags);
CREATE INDEX idx_lessons_severity ON lessons(severity);
CREATE INDEX idx_approaches_project ON approaches(project_id);
CREATE INDEX idx_approaches_status ON approaches(status);
CREATE INDEX idx_sessions_project ON sessions(project_id);
CREATE INDEX idx_sessions_machine ON sessions(machine_id);
CREATE INDEX idx_containers_project ON containers(project);
CREATE INDEX idx_containers_machine ON containers(machine_id);
CREATE INDEX idx_key_files_project ON key_files(project_id);
CREATE INDEX idx_permissions_scope ON permissions(scope);
CREATE INDEX idx_guardrails_project ON guardrails(project_id);
CREATE INDEX idx_journal_project ON journal(project_id);
CREATE INDEX idx_journal_tags ON journal USING gin(tags);
CREATE INDEX idx_journal_date ON journal(entry_date DESC);

-- ============================================
-- Helper Functions
-- ============================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Triggers for updated_at
CREATE TRIGGER update_machines_updated_at
    BEFORE UPDATE ON machines
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_project_state_updated_at
    BEFORE UPDATE ON project_state
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- Semantic Search Function
-- ============================================

-- Search across lessons, patterns, sessions, and journal by vector similarity
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
        WHERE l.embedding IS NOT NULL

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
