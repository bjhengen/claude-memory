# Claude Memory v3 Design — Codified Context Infrastructure

**Date:** 2026-03-01
**Status:** Approved
**Approach:** Flat extension (new tables and tool modules alongside existing ones)
**Inspiration:** [Codified Context: Infrastructure for AI Agents in a Complex Codebase](https://arxiv.org/abs/2602.20478) (Vasilopoulos, 2026)

## Goals

1. **Agent specifications registry** — Store, version, and retrieve reusable domain-expert agent definitions with semantic matching and keyword triggers
2. **Specification documents** — Structured long-form project knowledge (cold memory tier) beyond short lessons
3. **MCP server registry** — Catalog of MCP servers, their tools, machine constraints, and auth requirements across the ecosystem
4. **Unified tiered retrieval** — Single `find_context` tool that combines agents, specs, lessons, and MCP tools in one call
5. **Lifecycle management** — All new entities support soft delete (retire) with reason tracking, consistent with v2 lesson lifecycle

## Non-Goals

- No embedding model changes (still ada-002)
- No auth changes (OAuth stays as deployed)
- No changes to existing tool signatures or behavior
- No automatic memory formation (deferred to future work)
- No bidirectional file sync for agent specs (retrieval-only; CLAUDE.md instruction tells Claude to use `suggest_agent`)

## Architecture Decision: Retrieval-Only Agents

Agent specs are stored in the database and retrieved via MCP tools. They are NOT synced to local `.claude/agents/` files. Instead, a standing instruction in the global CLAUDE.md tells Claude to call `suggest_agent` when starting non-trivial tasks. If a match is found, Claude calls `get_agent` to load the full spec and follows it inline.

This avoids file sync complexity while making agents portable across machines.

## Schema

### Agent Specifications

```sql
CREATE TABLE agent_specs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    spec_content TEXT NOT NULL,
    summary TEXT,
    model VARCHAR(20) DEFAULT 'sonnet',
    triggers TEXT[] DEFAULT '{}',
    tools TEXT[] DEFAULT '{}',
    project_id INT REFERENCES projects(id),
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
CREATE INDEX idx_agent_specs_embedding ON agent_specs USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

Fields:
- `name`: Unique identifier (e.g., `network-protocol-designer`)
- `description`: One-line purpose
- `spec_content`: Full markdown specification (domain knowledge, rules, checklists — can be hundreds of lines)
- `summary`: 1-2 paragraph summary used for embedding generation and search result display
- `model`: Preferred Claude model (opus, sonnet, haiku)
- `triggers`: Keywords for auto-routing (e.g., `['network', 'sync', 'protocol']`)
- `tools`: Allowed tools (e.g., `['Read', 'Edit', 'Grep', 'Bash']`)
- `project_id`: NULL = global agent, set = project-specific
- `version`: Auto-incremented on update
- `retired_at` / `retired_reason`: Soft delete with reason tracking

Embedding is generated from `summary`, not `spec_content` (which may be too long for useful embeddings).

### Specification Documents

```sql
CREATE TABLE specifications (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    subsystem VARCHAR(100),
    content TEXT NOT NULL,
    summary TEXT,
    format_hints TEXT[] DEFAULT '{}',
    triggers TEXT[] DEFAULT '{}',
    project_id INT NOT NULL REFERENCES projects(id),
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
CREATE INDEX idx_specs_embedding ON specifications USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

Fields:
- `title`: Human-readable name (e.g., "Save System Architecture")
- `subsystem`: Domain tag (e.g., "networking", "auth", "deployment")
- `content`: Full structured markdown (no size limit)
- `summary`: 1-2 paragraph summary for embedding and search results
- `format_hints`: Content structure tags (`['decision-tree', 'symptom-fix-table', 'state-flow']`)
- `triggers`: Keywords for routing
- `project_id`: Required — specs are always project-scoped
- `verified_at`: Last confirmed accurate (tracks freshness)

Key differences from lessons:
- Always project-scoped (lessons can be global)
- `subsystem` for domain grouping
- `format_hints` so Claude knows what structure to expect before fetching full content
- `verified_at` tracks freshness (the paper's "stale specs mislead" principle)
- Designed for long-form content (100-1200+ lines) vs. lessons (1-3 paragraphs)

### MCP Server Registry

Three tables: servers, tools, and a project junction.

```sql
CREATE TABLE mcp_servers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    url TEXT,
    transport VARCHAR(20) NOT NULL,
    machine_id INT REFERENCES machines(id),
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

CREATE TABLE mcp_server_projects (
    server_id INT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (server_id, project_id)
);

CREATE INDEX idx_mcp_servers_machine ON mcp_servers(machine_id);
CREATE INDEX idx_mcp_servers_status ON mcp_servers(status);
CREATE INDEX idx_mcp_servers_retired ON mcp_servers(retired_at) WHERE retired_at IS NULL;
CREATE INDEX idx_mcp_servers_embedding ON mcp_servers USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_mcp_tools_server ON mcp_server_tools(server_id);
CREATE INDEX idx_mcp_tools_embedding ON mcp_server_tools USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

Fields (mcp_servers):
- `name`: Server identifier (e.g., `claude-memory`, `context7`)
- `description`: What this server provides
- `url`: Connection endpoint (NULL for stdio-based servers)
- `transport`: `stdio`, `sse`, `streamable-http`
- `machine_id`: Where it runs (NULL = cloud/anywhere). Links to existing machines table.
- `auth_type`: `api-key`, `oauth`, `none`
- `auth_hint`: How to authenticate (never store actual secrets)
- `config_snippet`: Ready-to-use JSON for settings files
- `limitations`: Runtime constraints, machine requirements, rate limits
- `status`: `active`, `deprecated`, `offline`
- `retired_at` / `retired_reason`: Soft delete

Fields (mcp_server_tools):
- `tool_name`: Tool identifier within the server
- `description`: What the tool does
- `parameters`: Full JSON parameter schema
- `embedding`: For semantic tool discovery via `find_mcp_tools`

## Tools

### Agent Specification Tools (src/tools/agents.py)

**`register_agent(name, description, spec_content, summary, model?, triggers?, tools?, project?, version?)`**
- Creates a new agent spec. Generates embedding from `summary`.
- Duplicate name → error with existing ID.

**`get_agent(name)`**
- Returns full agent spec including `spec_content`.
- This is what Claude reads when following an agent's instructions.

**`update_agent(agent_id, description?, spec_content?, summary?, model?, triggers?, tools?)`**
- Partial update of any combination of fields.
- Regenerates embedding if summary changes.
- Auto-increments version.

**`list_agents(project?, include_retired?)`**
- Lists agent specs at summary level (no spec_content).
- Optional project filter. Excludes retired by default.

**`retire_agent(agent_id, reason?)`**
- Soft delete via retired_at + reason.

**`suggest_agent(task_description, project?)`**
- Combined scoring: semantic similarity on embeddings + keyword matching on triggers.
- If project specified, searches both project-specific and global agents.
- Returns top 3 ranked matches with confidence (high/medium/low).
- Response includes name, description, model, confidence, matched_triggers.

### Specification Document Tools (src/tools/specs.py)

**`create_spec(title, content, summary, project, subsystem?, format_hints?, triggers?)`**
- Creates a new specification. Generates embedding from `summary`.
- Project is required.

**`get_spec(spec_id)`**
- Returns full content. This is the "cold memory retrieval" call.

**`update_spec(spec_id, title?, content?, summary?, subsystem?, format_hints?, triggers?)`**
- Partial update. Regenerates embedding if summary changes. Bumps version. Resets `verified_at`.

**`list_specs(project?, subsystem?, include_retired?)`**
- Lists specs at summary level. Filterable by project and/or subsystem.

**`retire_spec(spec_id, reason?)`**
- Soft delete.

**`search_specs(query, project?, subsystem?, limit?)`**
- Semantic search across spec summaries. Returns ranked results with similarity scores.

### MCP Server Registry Tools (src/tools/mcp_registry.py)

**`register_mcp_server(name, description, transport, url?, machine?, auth_type?, auth_hint?, config_snippet?, limitations?, projects?)`**
- Registers a server. Generates embedding from description. Links to projects via junction table.

**`get_mcp_server(name)`**
- Returns full server info including all its tools.

**`update_mcp_server(server_id, description?, url?, transport?, machine?, auth_type?, auth_hint?, config_snippet?, limitations?, status?)`**
- Partial update. Regenerates embedding if description changes.

**`retire_mcp_server(server_id, reason?)`**
- Soft delete.

**`list_mcp_servers(machine?, project?, status?, include_retired?)`**
- Lists servers with filters. Shows tool count per server.

**`register_mcp_tool(server, tool_name, description, parameters?)`**
- Adds a tool to a server's catalog. Generates embedding from description.
- Idempotent — updates if tool_name already exists for that server.

**`find_mcp_tools(query, project?, machine?, limit?)`**
- Semantic search across tool descriptions.
- Returns tools with parent server context (URL, machine, auth requirements).
- Answers "where can I do X?" across the whole MCP ecosystem.

### Unified Retrieval (added to src/tools/search.py)

**`find_context(query, project?, tiers?, limit_per_tier?)`**
- `query` (required): Natural language task description
- `project` (optional): Scope to a project
- `tiers` (optional): Array of `['agents', 'specs', 'lessons', 'mcp_tools']`. Defaults to all.
- `limit_per_tier` (optional): Max results per tier. Default 3.

Generates a single embedding for the query and reuses it across all tier searches. Returns:
```json
{
  "query": "fixing network sync bug in recipe.sync",
  "agents": [{"name": "...", "description": "...", "model": "...", "confidence": "high"}],
  "specifications": [{"title": "...", "subsystem": "...", "summary": "...", "similarity": 0.87}],
  "lessons": [{"title": "...", "content": "...", "similarity": 0.82}],
  "mcp_tools": [{"tool": "...", "server": "...", "description": "...", "similarity": 0.79}]
}
```

## Updated semantic_search Function

The existing PostgreSQL function adds new entity types to its UNION:

```sql
-- Add to existing UNION in semantic_search():
UNION ALL
SELECT 'agent_spec' AS source_type, id AS source_id, name AS title,
       COALESCE(summary, description) AS content, 1 - (embedding <=> query_embedding) AS similarity
FROM agent_specs WHERE retired_at IS NULL AND embedding IS NOT NULL
UNION ALL
SELECT 'specification', id, title,
       COALESCE(summary, title), 1 - (embedding <=> query_embedding)
FROM specifications WHERE retired_at IS NULL AND embedding IS NOT NULL
UNION ALL
SELECT 'mcp_tool', id, tool_name,
       description, 1 - (embedding <=> query_embedding)
FROM mcp_server_tools WHERE embedding IS NOT NULL
```

This means the existing `search()` tool automatically picks up new entity types with no tool changes.

## Migration

Single migration file: `migrations/003_v3_codified_context.sql`

Contents:
1. CREATE TABLE agent_specs (with all indexes)
2. CREATE TABLE specifications (with all indexes)
3. CREATE TABLE mcp_servers (with all indexes)
4. CREATE TABLE mcp_server_tools (with all indexes)
5. CREATE TABLE mcp_server_projects
6. CREATE OR REPLACE FUNCTION semantic_search (updated UNION)

## New Module Structure

```
src/tools/
  agents.py         # 6 tools: register, get, update, list, retire, suggest
  specs.py          # 6 tools: create, get, update, list, retire, search
  mcp_registry.py   # 7 tools: register_server, get, update, retire, list, register_tool, find_tools
  search.py         # existing + find_context
```

## Deployment

Same pattern as v2:
1. Database backup (pg_dump on EC2)
2. Copy backup locally
3. Apply migration 003
4. Rebuild container with new tool modules
5. Restart containers
6. Verify all existing 27 tools still work
7. Test new 19 tools
8. Seed initial data: register claude-memory itself as the first MCP server entry

Estimated downtime: ~2-3 minutes (same as v2).

## Tool Count

| Category | v2 | v3 New | v3 Total |
|----------|-----|--------|----------|
| Search | 2 | 1 | 3 |
| Projects | 6 | 0 | 6 |
| Infrastructure | 4 | 0 | 4 |
| Lessons | 4 | 0 | 4 |
| Sessions | 2 | 0 | 2 |
| Journal | 2 | 0 | 2 |
| Admin | 5 | 0 | 5 |
| Agents | 0 | 6 | 6 |
| Specifications | 0 | 6 | 6 |
| MCP Registry | 0 | 7 | 7 |
| **Total** | **27** | **19** | **46** |* Note: `add_project` counted under both Projects and Admin in v2.

## CLAUDE.md Integration

After deployment, add this standing instruction to the global CLAUDE.md:

```markdown
### Agent & Context Discovery
Before starting implementation on non-trivial tasks, call `suggest_agent(task_description)`
to check for relevant domain-expert specifications. If a match is returned with medium or
high confidence, call `get_agent(name)` and follow its instructions.

For unfamiliar subsystems, call `find_context(task_description)` to get relevant specs,
lessons, and available MCP tools in one call.
```

## Relationship to Paper's Architecture

| Paper Concept | claude-memory Equivalent |
|---------------|-------------------------|
| Hot memory (constitution) | Project CLAUDE.md (v2) |
| Warm memory (agent specs) | `agent_specs` table (v3) |
| Cold memory (spec docs) | `specifications` table (v3) |
| MCP retrieval service | `find_context` + `search_specs` + `find_mcp_tools` (v3) |
| Trigger tables | `triggers` arrays on agent_specs and specifications (v3) |
| suggest_agent | `suggest_agent` tool with semantic + keyword scoring (v3) |

Key advantage over the paper: Our implementation is database-backed with vector search (semantic retrieval) rather than flat files with keyword matching. Agents and specs are shared across machines and projects rather than per-project.

## Future Work (Not in Scope)

- Automatic memory formation (detect repeated explanations → suggest codification)
- Memory decay (contradiction-triggered retirement)
- Conflict detection across lessons/specs
- Spec drift detection (compare spec content against actual code)
- Factory agents for bootstrapping new projects (paper's quickstart concept)
