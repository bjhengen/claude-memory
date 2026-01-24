# Claude Memory System Design

**Date:** 2026-01-24
**Status:** Approved, ready for implementation

## Overview

A cross-machine, cross-project memory system for Claude Code sessions. Provides persistent storage for lessons learned, project context, infrastructure details, and session history - accessible from any machine via MCP protocol.

## Goals

1. **Session continuity** - Pick up where we left off without re-explaining context
2. **Knowledge capture** - Auto-save lessons and patterns from sessions
3. **Cross-project memory** - Patterns learned in one project surface in others

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  AWS EC2 (<YOUR_EC2_IP>)                                        │
│  ┌──────────────────┐    ┌──────────────────────────────────┐   │
│  │  PostgreSQL 15   │◄───│  claude-memory MCP server        │   │
│  │  + pgvector      │    │  (Python + MCP protocol)         │   │
│  │                  │    │                                  │   │
│  │  claude_memory   │    │  - Structured data queries       │   │
│  │  database        │    │  - Semantic search via pgvector  │   │
│  └──────────────────┘    │  - OpenAI ada-002 for embeddings │   │
│                          │  - API key authentication        │   │
│         Docker Compose (isolated from other workloads)       │   │
└─────────────────────────────────────────────────────────────────┘
```

## Database Schema

### Infrastructure & Connectivity

```sql
CREATE TABLE machines (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE,        -- 'mac-studio', 'work-laptop', 'slmbeast', 'aws-ec2'
    ip VARCHAR(50),
    ssh_command TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE databases (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    db_type VARCHAR(20),            -- 'postgresql', 'sqlite'
    machine_id INT REFERENCES machines(id),
    connection_hint TEXT,
    project VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE containers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    machine_id INT REFERENCES machines(id),
    compose_path TEXT,
    ports TEXT,
    project VARCHAR(100),
    status VARCHAR(20) DEFAULT 'running',
    created_at TIMESTAMP DEFAULT NOW()
);
```

### Projects & Current State

```sql
CREATE TABLE projects (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE,
    path TEXT,
    machine_id INT REFERENCES machines(id),
    status VARCHAR(20),             -- 'active', 'production', 'inactive'
    tech_stack JSONB,
    current_phase TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE approaches (
    id SERIAL PRIMARY KEY,
    project_id INT REFERENCES projects(id),
    area VARCHAR(100),
    current_approach TEXT,
    previous_approach TEXT,
    reason_for_change TEXT,
    changed_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'current'
);

CREATE TABLE key_files (
    id SERIAL PRIMARY KEY,
    project_id INT REFERENCES projects(id),
    file_path TEXT,
    line_hint INT,
    description TEXT,
    importance VARCHAR(20)          -- 'critical', 'important', 'reference'
);
```

### Permissions & Guardrails

```sql
CREATE TABLE permissions (
    id SERIAL PRIMARY KEY,
    scope VARCHAR(50),              -- 'global', 'project:recipe.sync'
    action_type VARCHAR(100),
    pattern TEXT,
    allowed BOOLEAN,
    requires_confirmation BOOLEAN,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE guardrails (
    id SERIAL PRIMARY KEY,
    project_id INT REFERENCES projects(id) NULL,
    description TEXT,
    check_type VARCHAR(50),         -- 'pre_build', 'pre_deploy', 'always'
    file_path TEXT,
    pattern TEXT,
    severity VARCHAR(20)            -- 'critical', 'warning'
);
```

### Lessons & Patterns (with Vector Search)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE lessons (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200),
    content TEXT,
    project_id INT REFERENCES projects(id) NULL,
    tags TEXT[],
    severity VARCHAR(20),           -- 'critical', 'important', 'tip'
    learned_at TIMESTAMP DEFAULT NOW(),
    embedding VECTOR(1536)
);

CREATE TABLE patterns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    problem TEXT,
    solution TEXT,
    code_example TEXT,
    applies_to TEXT[],
    created_at TIMESTAMP DEFAULT NOW(),
    embedding VECTOR(1536)
);

CREATE TABLE workflows (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    description TEXT,
    steps JSONB,
    tools_used TEXT[],
    project_id INT REFERENCES projects(id) NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
```

### Session History

```sql
CREATE TABLE sessions (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP DEFAULT NOW(),
    ended_at TIMESTAMP,
    machine_id INT REFERENCES machines(id),
    project_id INT REFERENCES projects(id) NULL,
    summary TEXT,
    embedding VECTOR(1536)
);

CREATE TABLE session_items (
    id SERIAL PRIMARY KEY,
    session_id INT REFERENCES sessions(id),
    item_type VARCHAR(50),          -- 'completed', 'in_progress', 'blocked', 'discovered'
    description TEXT,
    file_paths TEXT[],
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE project_state (
    project_id INT PRIMARY KEY REFERENCES projects(id),
    last_session_id INT REFERENCES sessions(id),
    current_focus TEXT,
    blockers TEXT[],
    next_steps TEXT[],
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### Indexes for Performance

```sql
-- Vector similarity search indexes
CREATE INDEX idx_lessons_embedding ON lessons USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_patterns_embedding ON patterns USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_sessions_embedding ON sessions USING ivfflat (embedding vector_cosine_ops);

-- Common query indexes
CREATE INDEX idx_lessons_project ON lessons(project_id);
CREATE INDEX idx_lessons_tags ON lessons USING gin(tags);
CREATE INDEX idx_approaches_project ON approaches(project_id);
CREATE INDEX idx_sessions_project ON sessions(project_id);
CREATE INDEX idx_containers_project ON containers(project);
```

## MCP Server Tools

| Tool | Purpose | Parameters |
|------|---------|------------|
| `search` | Semantic search across lessons, patterns, sessions | `query: str, limit: int = 5` |
| `get_project` | Full project context | `name: str` |
| `get_connectivity` | Servers, containers, databases for a project | `project: str` |
| `log_lesson` | Save a new lesson | `title, content, project?, tags?, severity?` |
| `log_pattern` | Save a reusable pattern | `name, problem, solution, code_example?, applies_to?` |
| `start_session` | Begin tracking a session | `machine, project?` |
| `end_session` | Complete session with summary | `session_id, summary, items[]` |
| `update_project_state` | Update focus, next steps | `project, current_focus?, blockers?, next_steps?` |
| `check_guardrails` | Verify safety before action | `project, action` |
| `add_machine` | Register a new machine | `name, ip?, ssh_command?` |
| `add_container` | Register a container | `name, machine, project, ports?, compose_path?` |

## Deployment

- Docker Compose on AWS EC2 (<YOUR_EC2_IP>)
- Separate network from existing workloads
- Exposed on dedicated port (e.g., 8003)
- API key authentication
- HTTPS via existing nginx reverse proxy

## Initial Data Population

Migrate existing knowledge from:
- `~/.claude/CLAUDE.md` (global context)
- `~/dev/recipe_sync/memories/` (project docs)
- `~/dev/wine_dine_pro/*.md` (project docs)

## Security

- API key required for all requests
- Key stored in Claude Code config on each machine
- Rate limiting to prevent abuse
- No sensitive credentials stored (connection hints, not passwords)
