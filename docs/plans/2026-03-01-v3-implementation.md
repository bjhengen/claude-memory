# v3 Codified Context Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add agent specifications, specification documents, MCP server registry, and unified tiered retrieval to the claude-memory MCP server.

**Architecture:** Flat extension of existing tables and tool modules. Three new tables (agent_specs, specifications, mcp_servers + mcp_server_tools + mcp_server_projects), four new tool modules, one migration. Follows established v2 patterns exactly.

**Tech Stack:** PostgreSQL + pgvector, FastMCP, asyncpg, OpenAI ada-002 embeddings

---

### Task 1: Database Backup

**Files:**
- None (remote operation)

**Step 1: Backup the live database**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com \
  "docker exec claude_memory_db pg_dump -U claude claude_memory" > backups/claude_memory_$(date +%Y%m%d_%H%M%S).sql
```

Expected: SQL dump file in `backups/` directory

**Step 2: Verify backup is non-empty**

```bash
wc -l backups/claude_memory_*.sql | tail -1
```

Expected: Several hundred+ lines

---

### Task 2: Write the Migration

**Files:**
- Create: `migrations/003_v3_codified_context.sql`

**Step 1: Write the migration file**

```sql
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
```

**Step 2: Commit**

```bash
git add migrations/003_v3_codified_context.sql
git commit -m "chore: add v3 migration for codified context tables"
```

---

### Task 3: Agent Specification Tools

**Files:**
- Create: `src/tools/agents.py`
- Modify: `src/server.py` (add import)

**Step 1: Create `src/tools/agents.py`**

```python
"""Agent specification tools for managing reusable domain-expert agent definitions."""

import json
from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def register_agent(
    name: str,
    description: str,
    spec_content: str,
    summary: str = None,
    model: str = "sonnet",
    triggers: list[str] = None,
    tools: list[str] = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Register a new agent specification.

    Args:
        name: Unique agent identifier (e.g., 'network-protocol-designer')
        description: One-line purpose of this agent
        spec_content: Full markdown specification (domain knowledge, rules, checklists)
        summary: 1-2 paragraph summary for search results (embedded for semantic matching)
        model: Preferred Claude model: opus, sonnet, haiku
        triggers: Keywords for auto-routing (e.g., ['network', 'sync', 'protocol'])
        tools: Allowed tools (e.g., ['Read', 'Edit', 'Grep', 'Bash'])
        project: Project name (optional, NULL = global agent)
    """
    app = ctx.request_context.lifespan_context

    # Check for duplicate
    existing = await app.db.fetchrow(
        "SELECT id FROM agent_specs WHERE name = $1", name
    )
    if existing:
        return json.dumps({
            "success": False,
            "agent_id": existing["id"],
            "message": f"Agent '{name}' already exists with id {existing['id']}. Use update_agent to modify."
        })

    # Resolve project
    project_id = None
    if project:
        project_id = await resolve_project_id(app.db, project)
        if not project_id:
            return json.dumps({"error": f"Project '{project}' not found"})

    # Generate embedding from summary (or description if no summary)
    embedding_text = summary or description
    embedding = await get_embedding(app.openai, embedding_text)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO agent_specs (name, description, spec_content, summary, model,
                                 triggers, tools, project_id, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector)
        RETURNING id
        """,
        name, description, spec_content, summary, model,
        triggers or [], tools or [], project_id, embedding_str
    )

    return json.dumps({
        "success": True,
        "agent_id": row["id"],
        "message": f"Agent '{name}' registered successfully"
    })


@mcp.tool()
async def get_agent(name: str, ctx: Context = None) -> str:
    """
    Get full agent specification by name. Returns the complete spec_content
    for Claude to follow as inline instructions.

    Args:
        name: Agent identifier
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        SELECT a.*, p.name as project_name
        FROM agent_specs a
        LEFT JOIN projects p ON a.project_id = p.id
        WHERE a.name = $1 AND a.retired_at IS NULL
        """,
        name
    )

    if not row:
        return json.dumps({"error": f"Agent '{name}' not found or is retired"})

    return json.dumps({
        "name": row["name"],
        "description": row["description"],
        "spec_content": row["spec_content"],
        "summary": row["summary"],
        "model": row["model"],
        "triggers": row["triggers"],
        "tools": row["tools"],
        "project": row["project_name"],
        "version": row["version"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
    })


@mcp.tool()
async def update_agent(
    agent_id: int,
    description: str = None,
    spec_content: str = None,
    summary: str = None,
    model: str = None,
    triggers: list[str] = None,
    tools: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Update an existing agent specification. Only provided fields are changed.
    Auto-increments version. Regenerates embedding if summary changes.

    Args:
        agent_id: ID of the agent to update
        description: New description
        spec_content: New full specification content
        summary: New summary (triggers re-embedding)
        model: New preferred model
        triggers: New trigger keywords
        tools: New allowed tools
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT * FROM agent_specs WHERE id = $1", agent_id
    )
    if not existing:
        return json.dumps({"error": f"Agent {agent_id} not found"})

    updates = ["version = version + 1", "updated_at = NOW()"]
    params = []
    param_idx = 1

    if description is not None:
        updates.append(f"description = ${param_idx}")
        params.append(description)
        param_idx += 1

    if spec_content is not None:
        updates.append(f"spec_content = ${param_idx}")
        params.append(spec_content)
        param_idx += 1

    if summary is not None:
        updates.append(f"summary = ${param_idx}")
        params.append(summary)
        param_idx += 1

    if model is not None:
        updates.append(f"model = ${param_idx}")
        params.append(model)
        param_idx += 1

    if triggers is not None:
        updates.append(f"triggers = ${param_idx}")
        params.append(triggers)
        param_idx += 1

    if tools is not None:
        updates.append(f"tools = ${param_idx}")
        params.append(tools)
        param_idx += 1

    if len(params) == 0:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if summary changed
    if summary is not None:
        embedding = await get_embedding(app.openai, summary)
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(agent_id)
    await app.db.execute(
        f"UPDATE agent_specs SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({
        "success": True,
        "message": f"Agent {agent_id} ('{existing['name']}') updated"
    })


@mcp.tool()
async def list_agents(
    project: str = None,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    List agent specifications. Returns summary-level info (not full spec_content).

    Args:
        project: Filter by project name (optional)
        include_retired: Include retired agents (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []
    param_idx = 1

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            # Include both project-specific and global agents
            conditions.append(f"(a.project_id = ${param_idx} OR a.project_id IS NULL)")
            params.append(project_id)
            param_idx += 1

    if not include_retired:
        conditions.append("a.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    rows = await app.db.fetch(
        f"""
        SELECT a.id, a.name, a.description, a.summary, a.model,
               a.triggers, a.tools, a.version, a.project_id,
               p.name as project_name, a.retired_at
        FROM agent_specs a
        LEFT JOIN projects p ON a.project_id = p.id
        WHERE {where_clause}
        ORDER BY a.name
        """
        + (f"" if not params else ""),
        *params
    )

    agents = []
    for row in rows:
        agents.append({
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "summary": row["summary"],
            "model": row["model"],
            "triggers": row["triggers"],
            "tools": row["tools"],
            "version": row["version"],
            "project": row["project_name"],
            "retired": row["retired_at"] is not None
        })

    return json.dumps({"agents": agents})


@mcp.tool()
async def retire_agent(
    agent_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire an agent specification (soft delete).
    Retired agents are excluded from suggest_agent and list_agents by default.

    Args:
        agent_id: ID of the agent to retire
        reason: Why the agent is being retired
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, name FROM agent_specs WHERE id = $1", agent_id
    )
    if not existing:
        return json.dumps({"error": f"Agent {agent_id} not found"})

    await app.db.execute(
        "UPDATE agent_specs SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, agent_id
    )

    return json.dumps({
        "success": True,
        "message": f"Agent {agent_id} ('{existing['name']}') retired"
    })


@mcp.tool()
async def suggest_agent(
    task_description: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Suggest which agent to use for a task. Combines semantic similarity
    on embeddings with keyword matching on triggers.

    Args:
        task_description: Description of the task you're about to do
        project: Project name to include project-specific agents
    """
    app = ctx.request_context.lifespan_context

    # Generate embedding for semantic search
    embedding = await get_embedding(app.openai, task_description)
    embedding_str = format_embedding(embedding)

    # Build project filter
    project_filter = "AND a.retired_at IS NULL"
    params = [embedding_str]
    param_idx = 2

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            project_filter += f" AND (a.project_id = ${param_idx} OR a.project_id IS NULL)"
            params.append(project_id)
            param_idx += 1

    # Semantic search
    rows = await app.db.fetch(
        f"""
        SELECT a.id, a.name, a.description, a.summary, a.model,
               a.triggers, a.tools, p.name as project_name,
               1 - (a.embedding <=> $1::vector) as similarity
        FROM agent_specs a
        LEFT JOIN projects p ON a.project_id = p.id
        WHERE a.embedding IS NOT NULL {project_filter}
        ORDER BY similarity DESC
        LIMIT 10
        """,
        *params
    )

    # Score: combine semantic similarity with trigger keyword matching
    task_lower = task_description.lower()
    task_words = set(task_lower.split())
    scored = []

    for row in rows:
        semantic_score = float(row["similarity"])
        trigger_score = 0.0
        matched_triggers = []

        for trigger in (row["triggers"] or []):
            if trigger.lower() in task_lower:
                trigger_score += 1.0
                matched_triggers.append(trigger)
            elif trigger.lower() in task_words:
                trigger_score += 0.5
                matched_triggers.append(trigger)

        # Weighted combination: 60% semantic, 40% trigger
        combined = (semantic_score * 0.6) + (min(trigger_score, 3.0) / 3.0 * 0.4)

        scored.append({
            "name": row["name"],
            "description": row["description"],
            "model": row["model"],
            "project": row["project_name"],
            "semantic_similarity": round(semantic_score, 3),
            "matched_triggers": matched_triggers,
            "combined_score": round(combined, 3)
        })

    scored.sort(key=lambda x: x["combined_score"], reverse=True)
    top_3 = scored[:3]

    # Determine confidence
    top_score = top_3[0]["combined_score"] if top_3 else 0
    confidence = "high" if top_score >= 0.7 else "medium" if top_score >= 0.5 else "low"

    return json.dumps({
        "task": task_description,
        "recommendation": top_3[0]["name"] if top_3 else None,
        "confidence": confidence,
        "suggested_agents": top_3
    })
```

**Step 2: Register in server.py**

Add this import at the end of `src/server.py` alongside existing tool imports:

```python
import src.tools.agents     # noqa: E402, F401
```

**Step 3: Commit**

```bash
git add src/tools/agents.py src/server.py
git commit -m "feat: add agent specification tools (register, get, update, list, retire, suggest)"
```

---

### Task 4: Specification Document Tools

**Files:**
- Create: `src/tools/specs.py`
- Modify: `src/server.py` (add import)

**Step 1: Create `src/tools/specs.py`**

```python
"""Specification document tools for managing structured project knowledge."""

import json
from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def create_spec(
    title: str,
    content: str,
    summary: str,
    project: str,
    subsystem: str = None,
    format_hints: list[str] = None,
    triggers: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Create a new specification document. Specs are structured long-form
    project knowledge (architecture docs, system designs, decision trees).

    Args:
        title: Document title (e.g., 'Save System Architecture')
        content: Full structured markdown content
        summary: 1-2 paragraph summary for search results (embedded for semantic matching)
        project: Project name (required — specs are always project-scoped)
        subsystem: Domain tag (e.g., 'networking', 'auth', 'deployment')
        format_hints: Content structure tags (e.g., ['decision-tree', 'symptom-fix-table'])
        triggers: Keywords for routing
    """
    app = ctx.request_context.lifespan_context

    project_id = await resolve_project_id(app.db, project)
    if not project_id:
        return json.dumps({"error": f"Project '{project}' not found"})

    # Generate embedding from summary
    embedding = await get_embedding(app.openai, summary)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO specifications (title, content, summary, project_id, subsystem,
                                    format_hints, triggers, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
        RETURNING id
        """,
        title, content, summary, project_id, subsystem,
        format_hints or [], triggers or [], embedding_str
    )

    return json.dumps({
        "success": True,
        "spec_id": row["id"],
        "message": f"Specification '{title}' created"
    })


@mcp.tool()
async def get_spec(spec_id: int, ctx: Context = None) -> str:
    """
    Get full specification content by ID. This is the cold-memory retrieval call —
    use when you need the detailed architecture/system documentation.

    Args:
        spec_id: ID of the specification to retrieve
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        SELECT s.*, p.name as project_name
        FROM specifications s
        LEFT JOIN projects p ON s.project_id = p.id
        WHERE s.id = $1
        """,
        spec_id
    )

    if not row:
        return json.dumps({"error": f"Specification {spec_id} not found"})

    return json.dumps({
        "id": row["id"],
        "title": row["title"],
        "subsystem": row["subsystem"],
        "content": row["content"],
        "summary": row["summary"],
        "format_hints": row["format_hints"],
        "triggers": row["triggers"],
        "project": row["project_name"],
        "version": row["version"],
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "retired": row["retired_at"] is not None
    })


@mcp.tool()
async def update_spec(
    spec_id: int,
    title: str = None,
    content: str = None,
    summary: str = None,
    subsystem: str = None,
    format_hints: list[str] = None,
    triggers: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Update a specification document. Only provided fields are changed.
    Bumps version. Resets verified_at to now. Regenerates embedding if summary changes.

    Args:
        spec_id: ID of the spec to update
        title: New title
        content: New full content
        summary: New summary (triggers re-embedding)
        subsystem: New subsystem tag
        format_hints: New format hint tags
        triggers: New trigger keywords
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT * FROM specifications WHERE id = $1", spec_id
    )
    if not existing:
        return json.dumps({"error": f"Specification {spec_id} not found"})

    updates = ["version = version + 1", "updated_at = NOW()", "verified_at = NOW()"]
    params = []
    param_idx = 1

    if title is not None:
        updates.append(f"title = ${param_idx}")
        params.append(title)
        param_idx += 1

    if content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(content)
        param_idx += 1

    if summary is not None:
        updates.append(f"summary = ${param_idx}")
        params.append(summary)
        param_idx += 1

    if subsystem is not None:
        updates.append(f"subsystem = ${param_idx}")
        params.append(subsystem)
        param_idx += 1

    if format_hints is not None:
        updates.append(f"format_hints = ${param_idx}")
        params.append(format_hints)
        param_idx += 1

    if triggers is not None:
        updates.append(f"triggers = ${param_idx}")
        params.append(triggers)
        param_idx += 1

    if len(params) == 0:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if summary changed
    if summary is not None:
        embedding = await get_embedding(app.openai, summary)
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(spec_id)
    await app.db.execute(
        f"UPDATE specifications SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({
        "success": True,
        "message": f"Specification {spec_id} ('{existing['title']}') updated"
    })


@mcp.tool()
async def list_specs(
    project: str = None,
    subsystem: str = None,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    List specification documents at summary level (no full content).

    Args:
        project: Filter by project name
        subsystem: Filter by subsystem tag
        include_retired: Include retired specs (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []
    param_idx = 1

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            conditions.append(f"s.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if subsystem:
        conditions.append(f"s.subsystem = ${param_idx}")
        params.append(subsystem)
        param_idx += 1

    if not include_retired:
        conditions.append("s.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    rows = await app.db.fetch(
        f"""
        SELECT s.id, s.title, s.subsystem, s.summary, s.format_hints,
               s.triggers, s.version, s.verified_at, s.project_id,
               p.name as project_name, s.retired_at
        FROM specifications s
        LEFT JOIN projects p ON s.project_id = p.id
        WHERE {where_clause}
        ORDER BY s.title
        """,
        *params
    )

    specs = []
    for row in rows:
        specs.append({
            "id": row["id"],
            "title": row["title"],
            "subsystem": row["subsystem"],
            "summary": row["summary"],
            "format_hints": row["format_hints"],
            "triggers": row["triggers"],
            "version": row["version"],
            "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
            "project": row["project_name"],
            "retired": row["retired_at"] is not None
        })

    return json.dumps({"specifications": specs})


@mcp.tool()
async def retire_spec(
    spec_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire a specification (soft delete).
    Retired specs are excluded from search and list by default.

    Args:
        spec_id: ID of the spec to retire
        reason: Why the spec is being retired
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, title FROM specifications WHERE id = $1", spec_id
    )
    if not existing:
        return json.dumps({"error": f"Specification {spec_id} not found"})

    await app.db.execute(
        "UPDATE specifications SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, spec_id
    )

    return json.dumps({
        "success": True,
        "message": f"Specification {spec_id} ('{existing['title']}') retired"
    })


@mcp.tool()
async def search_specs(
    query: str,
    project: str = None,
    subsystem: str = None,
    limit: int = 5,
    ctx: Context = None
) -> str:
    """
    Semantic search across specification summaries.

    Args:
        query: Natural language search query
        project: Filter by project name
        subsystem: Filter by subsystem tag
        limit: Maximum results (default 5)
    """
    app = ctx.request_context.lifespan_context

    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    conditions = ["s.embedding IS NOT NULL", "s.retired_at IS NULL"]
    params = [embedding_str]
    param_idx = 2

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            conditions.append(f"s.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if subsystem:
        conditions.append(f"s.subsystem = ${param_idx}")
        params.append(subsystem)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    params.append(limit)
    rows = await app.db.fetch(
        f"""
        SELECT s.id, s.title, s.subsystem, s.summary, s.format_hints,
               s.triggers, s.version, s.verified_at,
               p.name as project_name,
               1 - (s.embedding <=> $1::vector) as similarity
        FROM specifications s
        LEFT JOIN projects p ON s.project_id = p.id
        WHERE {where_clause}
        ORDER BY similarity DESC
        LIMIT ${param_idx}
        """,
        *params
    )

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "title": row["title"],
            "subsystem": row["subsystem"],
            "summary": row["summary"],
            "format_hints": row["format_hints"],
            "project": row["project_name"],
            "version": row["version"],
            "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
            "similarity": round(row["similarity"], 3)
        })

    return json.dumps({"specifications": results})
```

**Step 2: Register in server.py**

Add import to `src/server.py`:

```python
import src.tools.specs       # noqa: E402, F401
```

**Step 3: Commit**

```bash
git add src/tools/specs.py src/server.py
git commit -m "feat: add specification document tools (create, get, update, list, retire, search)"
```

---

### Task 5: MCP Server Registry Tools

**Files:**
- Create: `src/tools/mcp_registry.py`
- Modify: `src/server.py` (add import)

**Step 1: Create `src/tools/mcp_registry.py`**

```python
"""MCP server registry tools for cataloging servers, tools, and capabilities."""

import json
from mcp.server.fastmcp import Context

from src.server import mcp
from src.db import get_embedding, format_embedding
from src.helpers import resolve_project_id


@mcp.tool()
async def register_mcp_server(
    name: str,
    description: str,
    transport: str,
    url: str = None,
    machine: str = None,
    auth_type: str = "none",
    auth_hint: str = None,
    config_snippet: dict = None,
    limitations: str = None,
    projects: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Register an MCP server in the catalog.

    Args:
        name: Server identifier (e.g., 'claude-memory', 'context7')
        description: What this server provides
        transport: Connection type: 'stdio', 'sse', 'streamable-http'
        url: Connection endpoint (NULL for stdio)
        machine: Machine name where it runs (NULL = cloud/anywhere)
        auth_type: Authentication: 'api-key', 'oauth', 'none'
        auth_hint: How to authenticate (do not store secrets)
        config_snippet: Example config JSON for settings files
        limitations: Runtime constraints, rate limits, machine requirements
        projects: List of project names this server serves
    """
    app = ctx.request_context.lifespan_context

    # Check for duplicate
    existing = await app.db.fetchrow(
        "SELECT id FROM mcp_servers WHERE name = $1", name
    )
    if existing:
        return json.dumps({
            "success": False,
            "server_id": existing["id"],
            "message": f"Server '{name}' already exists with id {existing['id']}. Use update_mcp_server to modify."
        })

    # Resolve machine
    machine_id = None
    if machine:
        machine_row = await app.db.fetchrow(
            "SELECT id FROM machines WHERE name = $1", machine
        )
        if machine_row:
            machine_id = machine_row["id"]

    # Generate embedding
    embedding = await get_embedding(app.openai, description)
    embedding_str = format_embedding(embedding)

    # Convert config_snippet to JSON string for JSONB
    config_json = json.dumps(config_snippet) if config_snippet else None

    row = await app.db.fetchrow(
        """
        INSERT INTO mcp_servers (name, description, url, transport, machine_id,
                                 auth_type, auth_hint, config_snippet, limitations, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::vector)
        RETURNING id
        """,
        name, description, url, transport, machine_id,
        auth_type, auth_hint, config_json, limitations, embedding_str
    )

    server_id = row["id"]

    # Link to projects
    if projects:
        for proj_name in projects:
            project_id = await resolve_project_id(app.db, proj_name)
            if project_id:
                await app.db.execute(
                    """
                    INSERT INTO mcp_server_projects (server_id, project_id)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    """,
                    server_id, project_id
                )

    return json.dumps({
        "success": True,
        "server_id": server_id,
        "message": f"MCP server '{name}' registered"
    })


@mcp.tool()
async def get_mcp_server(name: str, ctx: Context = None) -> str:
    """
    Get full MCP server info including all its tools.

    Args:
        name: Server identifier
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        SELECT s.*, m.name as machine_name, m.ip as machine_ip,
               m.ssh_command as machine_ssh
        FROM mcp_servers s
        LEFT JOIN machines m ON s.machine_id = m.id
        WHERE s.name = $1
        """,
        name
    )

    if not row:
        return json.dumps({"error": f"MCP server '{name}' not found"})

    # Get tools
    tools = await app.db.fetch(
        """
        SELECT tool_name, description, parameters
        FROM mcp_server_tools
        WHERE server_id = $1
        ORDER BY tool_name
        """,
        row["id"]
    )

    # Get linked projects
    projects = await app.db.fetch(
        """
        SELECT p.name
        FROM mcp_server_projects sp
        JOIN projects p ON sp.project_id = p.id
        WHERE sp.server_id = $1
        """,
        row["id"]
    )

    return json.dumps({
        "name": row["name"],
        "description": row["description"],
        "url": row["url"],
        "transport": row["transport"],
        "machine": row["machine_name"],
        "machine_ip": row["machine_ip"],
        "machine_ssh": row["machine_ssh"],
        "auth_type": row["auth_type"],
        "auth_hint": row["auth_hint"],
        "config_snippet": json.loads(row["config_snippet"]) if row["config_snippet"] else None,
        "limitations": row["limitations"],
        "status": row["status"],
        "retired": row["retired_at"] is not None,
        "tools": [
            {
                "name": t["tool_name"],
                "description": t["description"],
                "parameters": json.loads(t["parameters"]) if t["parameters"] else None
            }
            for t in tools
        ],
        "projects": [p["name"] for p in projects],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
    })


@mcp.tool()
async def update_mcp_server(
    server_id: int,
    description: str = None,
    url: str = None,
    transport: str = None,
    machine: str = None,
    auth_type: str = None,
    auth_hint: str = None,
    config_snippet: dict = None,
    limitations: str = None,
    status: str = None,
    ctx: Context = None
) -> str:
    """
    Update an MCP server registration. Only provided fields are changed.

    Args:
        server_id: ID of the server to update
        description: New description (triggers re-embedding)
        url: New connection URL
        transport: New transport type
        machine: New machine name
        auth_type: New auth type
        auth_hint: New auth hint
        config_snippet: New config JSON
        limitations: New limitations text
        status: New status: 'active', 'deprecated', 'offline'
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT * FROM mcp_servers WHERE id = $1", server_id
    )
    if not existing:
        return json.dumps({"error": f"MCP server {server_id} not found"})

    updates = ["updated_at = NOW()"]
    params = []
    param_idx = 1

    if description is not None:
        updates.append(f"description = ${param_idx}")
        params.append(description)
        param_idx += 1

    if url is not None:
        updates.append(f"url = ${param_idx}")
        params.append(url)
        param_idx += 1

    if transport is not None:
        updates.append(f"transport = ${param_idx}")
        params.append(transport)
        param_idx += 1

    if machine is not None:
        machine_row = await app.db.fetchrow(
            "SELECT id FROM machines WHERE name = $1", machine
        )
        if machine_row:
            updates.append(f"machine_id = ${param_idx}")
            params.append(machine_row["id"])
            param_idx += 1

    if auth_type is not None:
        updates.append(f"auth_type = ${param_idx}")
        params.append(auth_type)
        param_idx += 1

    if auth_hint is not None:
        updates.append(f"auth_hint = ${param_idx}")
        params.append(auth_hint)
        param_idx += 1

    if config_snippet is not None:
        updates.append(f"config_snippet = ${param_idx}::jsonb")
        params.append(json.dumps(config_snippet))
        param_idx += 1

    if limitations is not None:
        updates.append(f"limitations = ${param_idx}")
        params.append(limitations)
        param_idx += 1

    if status is not None:
        updates.append(f"status = ${param_idx}")
        params.append(status)
        param_idx += 1

    if len(params) == 0:
        return json.dumps({"error": "No updates provided"})

    # Regenerate embedding if description changed
    if description is not None:
        embedding = await get_embedding(app.openai, description)
        embedding_str = format_embedding(embedding)
        updates.append(f"embedding = ${param_idx}::vector")
        params.append(embedding_str)
        param_idx += 1

    params.append(server_id)
    await app.db.execute(
        f"UPDATE mcp_servers SET {', '.join(updates)} WHERE id = ${param_idx}",
        *params
    )

    return json.dumps({
        "success": True,
        "message": f"MCP server {server_id} ('{existing['name']}') updated"
    })


@mcp.tool()
async def retire_mcp_server(
    server_id: int,
    reason: str = None,
    ctx: Context = None
) -> str:
    """
    Retire an MCP server (soft delete).

    Args:
        server_id: ID of the server to retire
        reason: Why the server is being retired
    """
    app = ctx.request_context.lifespan_context

    existing = await app.db.fetchrow(
        "SELECT id, name FROM mcp_servers WHERE id = $1", server_id
    )
    if not existing:
        return json.dumps({"error": f"MCP server {server_id} not found"})

    await app.db.execute(
        "UPDATE mcp_servers SET retired_at = NOW(), retired_reason = $1 WHERE id = $2",
        reason, server_id
    )

    return json.dumps({
        "success": True,
        "message": f"MCP server {server_id} ('{existing['name']}') retired"
    })


@mcp.tool()
async def list_mcp_servers(
    machine: str = None,
    project: str = None,
    status: str = None,
    include_retired: bool = False,
    ctx: Context = None
) -> str:
    """
    List MCP servers with optional filters.

    Args:
        machine: Filter by machine name
        project: Filter by project name
        status: Filter by status: 'active', 'deprecated', 'offline'
        include_retired: Include retired servers (default False)
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []
    param_idx = 1
    joins = ""

    if machine:
        conditions.append(f"m.name = ${param_idx}")
        params.append(machine)
        param_idx += 1

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            joins = "JOIN mcp_server_projects sp ON s.id = sp.server_id"
            conditions.append(f"sp.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if status:
        conditions.append(f"s.status = ${param_idx}")
        params.append(status)
        param_idx += 1

    if not include_retired:
        conditions.append("s.retired_at IS NULL")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    rows = await app.db.fetch(
        f"""
        SELECT DISTINCT s.id, s.name, s.description, s.url, s.transport,
               s.auth_type, s.status, s.limitations,
               m.name as machine_name, s.retired_at,
               (SELECT COUNT(*) FROM mcp_server_tools WHERE server_id = s.id) as tool_count
        FROM mcp_servers s
        LEFT JOIN machines m ON s.machine_id = m.id
        {joins}
        WHERE {where_clause}
        ORDER BY s.name
        """,
        *params
    )

    servers = []
    for row in rows:
        servers.append({
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "url": row["url"],
            "transport": row["transport"],
            "auth_type": row["auth_type"],
            "status": row["status"],
            "limitations": row["limitations"],
            "machine": row["machine_name"],
            "tool_count": row["tool_count"],
            "retired": row["retired_at"] is not None
        })

    return json.dumps({"servers": servers})


@mcp.tool()
async def register_mcp_tool(
    server: str,
    tool_name: str,
    description: str,
    parameters: dict = None,
    ctx: Context = None
) -> str:
    """
    Register a tool in an MCP server's catalog. Idempotent — updates if
    tool already exists for that server.

    Args:
        server: MCP server name
        tool_name: Tool identifier (e.g., 'search', 'log_lesson')
        description: What the tool does
        parameters: Parameter schema as JSON object
    """
    app = ctx.request_context.lifespan_context

    server_row = await app.db.fetchrow(
        "SELECT id FROM mcp_servers WHERE name = $1", server
    )
    if not server_row:
        return json.dumps({"error": f"MCP server '{server}' not found"})

    # Generate embedding
    embedding = await get_embedding(app.openai, description)
    embedding_str = format_embedding(embedding)

    params_json = json.dumps(parameters) if parameters else None

    row = await app.db.fetchrow(
        """
        INSERT INTO mcp_server_tools (server_id, tool_name, description, parameters, embedding)
        VALUES ($1, $2, $3, $4::jsonb, $5::vector)
        ON CONFLICT (server_id, tool_name) DO UPDATE SET
            description = EXCLUDED.description,
            parameters = EXCLUDED.parameters,
            embedding = EXCLUDED.embedding
        RETURNING id
        """,
        server_row["id"], tool_name, description, params_json, embedding_str
    )

    return json.dumps({
        "success": True,
        "tool_id": row["id"],
        "message": f"Tool '{tool_name}' registered for server '{server}'"
    })


@mcp.tool()
async def find_mcp_tools(
    query: str,
    project: str = None,
    machine: str = None,
    limit: int = 5,
    ctx: Context = None
) -> str:
    """
    Search for MCP tools across all servers by semantic similarity.
    Answers 'where can I do X?' across the whole MCP ecosystem.

    Args:
        query: What capability you're looking for (e.g., 'search documents')
        project: Filter to servers that serve this project
        machine: Filter to servers on this machine
        limit: Maximum results (default 5)
    """
    app = ctx.request_context.lifespan_context

    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    conditions = [
        "t.embedding IS NOT NULL",
        "s.retired_at IS NULL"
    ]
    params = [embedding_str]
    param_idx = 2
    joins = ""

    if project:
        project_id = await resolve_project_id(app.db, project)
        if project_id:
            joins = "JOIN mcp_server_projects sp ON s.id = sp.server_id"
            conditions.append(f"sp.project_id = ${param_idx}")
            params.append(project_id)
            param_idx += 1

    if machine:
        conditions.append(f"m.name = ${param_idx}")
        params.append(machine)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    params.append(limit)
    rows = await app.db.fetch(
        f"""
        SELECT t.tool_name, t.description as tool_description, t.parameters,
               s.name as server_name, s.description as server_description,
               s.url, s.transport, s.auth_type, s.auth_hint,
               m.name as machine_name,
               1 - (t.embedding <=> $1::vector) as similarity
        FROM mcp_server_tools t
        JOIN mcp_servers s ON t.server_id = s.id
        LEFT JOIN machines m ON s.machine_id = m.id
        {joins}
        WHERE {where_clause}
        ORDER BY similarity DESC
        LIMIT ${param_idx}
        """,
        *params
    )

    results = []
    for row in rows:
        results.append({
            "tool": row["tool_name"],
            "tool_description": row["tool_description"],
            "parameters": json.loads(row["parameters"]) if row["parameters"] else None,
            "server": row["server_name"],
            "server_description": row["server_description"],
            "url": row["url"],
            "transport": row["transport"],
            "auth_type": row["auth_type"],
            "auth_hint": row["auth_hint"],
            "machine": row["machine_name"],
            "similarity": round(row["similarity"], 3)
        })

    return json.dumps({"tools": results})
```

**Step 2: Register in server.py**

Add import to `src/server.py`:

```python
import src.tools.mcp_registry  # noqa: E402, F401
```

**Step 3: Commit**

```bash
git add src/tools/mcp_registry.py src/server.py
git commit -m "feat: add MCP server registry tools (register, get, update, retire, list, register_tool, find)"
```

---

### Task 6: Unified Tiered Retrieval

**Files:**
- Modify: `src/tools/search.py` (add `find_context`)

**Step 1: Add `find_context` to search.py**

Append this function to the existing `src/tools/search.py`:

```python
@mcp.tool()
async def find_context(
    query: str,
    project: str = None,
    tiers: list[str] = None,
    limit_per_tier: int = 3,
    ctx: Context = None
) -> str:
    """
    Unified tiered retrieval. Searches across agents, specifications, lessons,
    and MCP tools in one call. Use this to orient yourself on a task.

    Args:
        query: Natural language task description
        project: Scope to a project
        tiers: Which tiers to search: ['agents', 'specs', 'lessons', 'mcp_tools']. Defaults to all.
        limit_per_tier: Max results per tier (default 3)
    """
    app = ctx.request_context.lifespan_context

    # Generate a single embedding for all tier searches
    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    # Resolve project if provided
    project_id = None
    if project:
        project_id = await resolve_project_id(app.db, project)

    active_tiers = tiers or ["agents", "specs", "lessons", "mcp_tools"]
    result = {"query": query}

    # --- Agents tier ---
    if "agents" in active_tiers:
        agent_conditions = ["a.embedding IS NOT NULL", "a.retired_at IS NULL"]
        agent_params = [embedding_str]
        agent_idx = 2

        if project_id:
            agent_conditions.append(f"(a.project_id = ${agent_idx} OR a.project_id IS NULL)")
            agent_params.append(project_id)
            agent_idx += 1

        agent_where = " AND ".join(agent_conditions)
        agent_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT a.name, a.description, a.model, a.triggers,
                   p.name as project_name,
                   1 - (a.embedding <=> $1::vector) as similarity
            FROM agent_specs a
            LEFT JOIN projects p ON a.project_id = p.id
            WHERE {agent_where}
            ORDER BY similarity DESC
            LIMIT ${agent_idx}
            """,
            *agent_params
        )

        # Add trigger keyword scoring
        task_lower = query.lower()
        agents = []
        for row in rows:
            matched = [t for t in (row["triggers"] or []) if t.lower() in task_lower]
            semantic = float(row["similarity"])
            trigger_bonus = min(len(matched), 3) / 3.0 * 0.4
            combined = semantic * 0.6 + trigger_bonus
            confidence = "high" if combined >= 0.7 else "medium" if combined >= 0.5 else "low"
            agents.append({
                "name": row["name"],
                "description": row["description"],
                "model": row["model"],
                "project": row["project_name"],
                "confidence": confidence,
                "similarity": round(combined, 3),
                "matched_triggers": matched
            })
        agents.sort(key=lambda x: x["similarity"], reverse=True)
        result["agents"] = agents

    # --- Specifications tier ---
    if "specs" in active_tiers:
        spec_conditions = ["s.embedding IS NOT NULL", "s.retired_at IS NULL"]
        spec_params = [embedding_str]
        spec_idx = 2

        if project_id:
            spec_conditions.append(f"s.project_id = ${spec_idx}")
            spec_params.append(project_id)
            spec_idx += 1

        spec_where = " AND ".join(spec_conditions)
        spec_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT s.id, s.title, s.subsystem, s.summary, s.format_hints,
                   p.name as project_name,
                   1 - (s.embedding <=> $1::vector) as similarity
            FROM specifications s
            LEFT JOIN projects p ON s.project_id = p.id
            WHERE {spec_where}
            ORDER BY similarity DESC
            LIMIT ${spec_idx}
            """,
            *spec_params
        )

        result["specifications"] = [
            {
                "id": row["id"],
                "title": row["title"],
                "subsystem": row["subsystem"],
                "summary": row["summary"],
                "format_hints": row["format_hints"],
                "project": row["project_name"],
                "similarity": round(row["similarity"], 3)
            }
            for row in rows
        ]

    # --- Lessons tier ---
    if "lessons" in active_tiers:
        lesson_conditions = ["l.embedding IS NOT NULL", "l.retired_at IS NULL"]
        lesson_params = [embedding_str]
        lesson_idx = 2

        if project_id:
            lesson_conditions.append(f"l.project_id = ${lesson_idx}")
            lesson_params.append(project_id)
            lesson_idx += 1

        lesson_where = " AND ".join(lesson_conditions)
        lesson_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT l.id, l.title, l.content, l.tags, l.severity,
                   p.name as project_name,
                   1 - (l.embedding <=> $1::vector) as similarity
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {lesson_where}
            ORDER BY similarity DESC
            LIMIT ${lesson_idx}
            """,
            *lesson_params
        )

        result["lessons"] = [
            {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"][:300] if row["content"] else None,
                "tags": row["tags"],
                "severity": row["severity"],
                "project": row["project_name"],
                "similarity": round(row["similarity"], 3)
            }
            for row in rows
        ]

    # --- MCP tools tier ---
    if "mcp_tools" in active_tiers:
        mcp_conditions = ["t.embedding IS NOT NULL", "s.retired_at IS NULL"]
        mcp_params = [embedding_str]
        mcp_idx = 2
        mcp_joins = ""

        if project_id:
            mcp_joins = "JOIN mcp_server_projects sp ON s.id = sp.server_id"
            mcp_conditions.append(f"sp.project_id = ${mcp_idx}")
            mcp_params.append(project_id)
            mcp_idx += 1

        mcp_where = " AND ".join(mcp_conditions)
        mcp_params.append(limit_per_tier)

        rows = await app.db.fetch(
            f"""
            SELECT t.tool_name, t.description as tool_description,
                   s.name as server_name, s.url, s.transport,
                   m.name as machine_name,
                   1 - (t.embedding <=> $1::vector) as similarity
            FROM mcp_server_tools t
            JOIN mcp_servers s ON t.server_id = s.id
            LEFT JOIN machines m ON s.machine_id = m.id
            {mcp_joins}
            WHERE {mcp_where}
            ORDER BY similarity DESC
            LIMIT ${mcp_idx}
            """,
            *mcp_params
        )

        result["mcp_tools"] = [
            {
                "tool": row["tool_name"],
                "description": row["tool_description"],
                "server": row["server_name"],
                "url": row["url"],
                "transport": row["transport"],
                "machine": row["machine_name"],
                "similarity": round(row["similarity"], 3)
            }
            for row in rows
        ]

    return json.dumps(result)
```

**Step 2: Commit**

```bash
git add src/tools/search.py
git commit -m "feat: add find_context unified tiered retrieval tool"
```

---

### Task 7: Deploy to Production

**Files:**
- None (remote operations)

**Step 1: Push code to GitHub**

```bash
git push origin main
```

**Step 2: Upload new files to EC2**

```bash
# Upload new tool modules
scp -i ~/.ssh/AWS_FR.pem src/tools/agents.py ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/tools/specs.py ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/tools/mcp_registry.py ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com:~/claude-memory/src/tools/

# Upload modified files
scp -i ~/.ssh/AWS_FR.pem src/tools/search.py ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com:~/claude-memory/src/tools/
scp -i ~/.ssh/AWS_FR.pem src/server.py ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com:~/claude-memory/src/

# Upload migration
scp -i ~/.ssh/AWS_FR.pem migrations/003_v3_codified_context.sql ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com:~/claude-memory/migrations/
```

**Step 3: Apply migration on EC2**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com \
  "docker exec -i claude_memory_db psql -U claude claude_memory < ~/claude-memory/migrations/003_v3_codified_context.sql"
```

Expected: Table creation confirmations, no errors

**Step 4: Rebuild and restart the MCP container**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com \
  "cd ~/claude-memory && docker-compose up -d --build mcp"
```

**Step 5: Verify health**

```bash
curl -s https://memory.friendly-robots.com/health
```

Expected: `{"status": "healthy"}`

**Step 6: Restart Claude Code to pick up new tools**

Quit and reopen Claude Code so the MCP client refreshes its tool list. Then verify new tools appear:
- Call `list_agents()` — should return empty list
- Call `list_specs()` — should return empty list
- Call `list_mcp_servers()` — should return empty list

---

### Task 8: Verify All Existing Tools Still Work

**Step 1: Test existing search**

Call `search("flutter build")` — should return results from existing lessons.

**Step 2: Test existing project tools**

Call `get_project("claude-memory")` — should return full project context.

**Step 3: Test existing lesson tools**

Call `search_lessons(project="claude-memory", limit=3)` — should return lessons.

---

### Task 9: Seed Initial Data

**Step 1: Register claude-memory as the first MCP server**

Call `register_mcp_server`:
- name: `claude-memory`
- description: `Cross-machine persistent memory for Claude sessions. Stores lessons, patterns, journal entries, project context, agent specs, specification documents, and MCP server catalog.`
- transport: `streamable-http`
- url: `https://memory.friendly-robots.com/mcp`
- machine: `aws-ec2` (or whatever the machine name is)
- auth_type: `oauth`
- auth_hint: `OAuth 2.0 via Claude Desktop auto-registration, or API key via CLAUDE_MEMORY_API_KEY header`
- projects: all active projects

**Step 2: Register claude-memory's tools in its own catalog**

Call `register_mcp_tool` for each of the 46 tools. This can be scripted or done in batches. Each call needs server name, tool name, and description.

**Step 3: Commit any seeding scripts**

```bash
git commit -m "chore: seed initial MCP server catalog data"
```

---

### Task 10: Update Global CLAUDE.md

**Step 1: Add agent/context discovery instructions**

Add to `~/.claude/CLAUDE.md` under a new section:

```markdown
### Agent & Context Discovery

Before starting implementation on non-trivial tasks, call `suggest_agent(task_description)`
to check for relevant domain-expert specifications. If a match is returned with medium or
high confidence, call `get_agent(name)` and follow its instructions.

For unfamiliar subsystems, call `find_context(task_description)` to get relevant specs,
lessons, and available MCP tools in one call.

When building or deploying MCP servers, register them via `register_mcp_server` and catalog
their tools via `register_mcp_tool` so they're discoverable across sessions and machines.
```

**Step 2: Commit**

No git commit needed — this is a user config file, not in the repo.

---

## Summary

| Task | Files | New Tools |
|------|-------|-----------|
| 1. Backup | — | — |
| 2. Migration | `migrations/003_v3_codified_context.sql` | — |
| 3. Agent tools | `src/tools/agents.py` | 6 |
| 4. Spec tools | `src/tools/specs.py` | 6 |
| 5. MCP registry tools | `src/tools/mcp_registry.py` | 7 |
| 6. Unified retrieval | `src/tools/search.py` (modify) | 1 |
| 7. Deploy | — (remote) | — |
| 8. Verify existing | — | — |
| 9. Seed data | — | — |
| 10. Update CLAUDE.md | `~/.claude/CLAUDE.md` (modify) | — |

**Total: 20 new tools, 3 new files, 1 migration, 1 modified file**
