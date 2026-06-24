# Claude Memory

A cross-machine, **cross-agent**, cross-project memory system for AI coding sessions. Provides persistent, attributed storage for lessons learned, project context, infrastructure details, codified agent/spec knowledge, an MCP server registry, a consolidation pipeline, and a reflective journal — shared across every machine and agent that connects.

Originally built for Claude Code; as of **v6** it is multi-agent (Claude + Codex) with per-write attribution.

## Features

- **Semantic + keyword search** — hybrid, confidence-weighted ranking over lessons, patterns, and sessions
- **Project context** — approaches, key files, guardrails, per-project `CLAUDE.md` storage, aliases
- **Infrastructure tracking** — machines, containers, databases, SSH connectivity
- **Session history** — log sessions and pick up where you left off
- **Codified context (v3)** — reusable agent specifications, long-form spec documents, and a unified `find_context` retrieval tool
- **MCP server registry (v3)** — catalog of MCP servers/tools, discoverable via `find_mcp_tools`
- **Feedback loop (v4)** — lesson up/down ratings affect search ranking; polymorphic annotations on any entity
- **Consolidation pipeline (v5)** — automatic duplicate/supersede detection with an auditable human-review queue
- **Multi-agent attribution (v6)** — every write stamped with `source_agent` + `source_client_id`; per-machine bearer tokens; cross-agent auto-merge is skipped to prevent silent corpus drift

## Architecture

- **PostgreSQL 16** (`pgvector/pgvector:pg16`) for storage + semantic search
- **Python 3.11** MCP server using FastMCP, served over HTTP
- **OpenAI ada-002** for embeddings
- **Docker Compose** deployment behind nginx on AWS EC2
- **57 MCP tools** across 14 functional areas (see below)

### Multi-agent identity (v6)

Authentication resolves an identity in this order:

1. **`api_keys` table** (per-machine/per-agent bearer tokens) — hash-matched, identifies the calling agent via `family` / `client_name` / `label`
2. **OAuth access tokens** (with expiry filtering)
3. **Legacy `CLAUDE_MEMORY_API_KEY`** shared bearer — *deprecated, back-compat only, slated for removal*

Every write tool stamps `source_agent` (e.g. `claude`, `codex`) and `source_client_id` onto the row. Shared-metadata writes use last-writer-wins; owned-content updates/retires enforce rule-b (an agent may only mutate its own content unless it has admin scope). Cross-agent pairs are skipped by the consolidation pipeline.

Per-machine tokens are issued/revoked/listed with the admin scripts in `scripts/` (`issue_api_key.py`, `revoke_api_key.py`, `list_api_keys.py`).

## Quick Start

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env with your values:
# - POSTGRES_PASSWORD
# - OPENAI_API_KEY
# - CLAUDE_MEMORY_API_KEY   (legacy shared bearer; v6 prefers per-machine api_keys)
```

### 2. Deploy to AWS

```bash
./deploy.sh
```

> Note: production is an rsync-managed directory on EC2, **not** a git checkout. `deploy.sh` handles the sync + container rebuild.

### 3. Configure nginx (on EC2)

Add the contents of `nginx-snippet.conf` to your nginx server block, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Database & Migrations

The base schema is `db/schema.sql`. Versioned migrations live in `db/migrations/` and are applied in order:

```
001_add_journal.sql
v4_feedback_loop.sql
v5_consolidation.sql
v5_1_backlog_analysis.sql
v5_oauth_persistence.sql
v6_attribution.sql
```

Seed initial data:

```bash
export DATABASE_URL="postgresql://claude:YOUR_PASSWORD@<YOUR_EC2_IP>:5433/claude_memory"
export OPENAI_API_KEY="sk-your-key"
python scripts/seed_data.py
```

### 5. Issue a per-machine token (v6)

```bash
# On the server, against the prod DB
python scripts/issue_api_key.py --label "Workstation" --client-name claude-code --family claude
python scripts/list_api_keys.py
```

### 6. Configure the MCP client

On each machine, add the MCP server using the Claude Code CLI (Codex uses its analogous config):

```bash
# Add with user scope (available in all projects)
claude mcp add -s user -t http claude-memory https://your-domain.com/mcp \
  -H "Authorization: Bearer YOUR_PER_MACHINE_TOKEN"

# Verify it's connected
claude mcp list
```

**Scope options:** `-s user` (all projects, recommended) · `-s local` (current dir) · `-s project` (current project). Config is stored in `~/.claude.json`.

## MCP Tools (57)

### Search & retrieval
| Tool | Description |
|------|-------------|
| `search` | Hybrid semantic+keyword search across lessons, patterns, sessions |
| `search_lessons` | Search lessons with filters |
| `find_context` | Unified tiered retrieval across agents, specs, lessons, MCP tools |

### Lessons & patterns
| Tool | Description |
|------|-------------|
| `log_lesson` | Save a new lesson learned |
| `log_pattern` | Save a reusable pattern |
| `update_lesson` | Edit an existing lesson |
| `retire_lesson` | Retire a stale/wrong lesson |
| `rate_lesson` | Up/down vote a lesson (affects search ranking) |

### Projects
| Tool | Description |
|------|-------------|
| `get_project` | Full project context (state, files, approaches) |
| `list_projects` | List all projects |
| `add_project` | Register a new project |
| `update_project_state` | Update focus/blockers/next_steps |
| `get_project_claude_md` / `set_project_claude_md` / `update_project_claude_md` | Per-project CLAUDE.md storage |
| `merge_projects` | Merge duplicate projects *(admin scope)* |

### Sessions & journal
| Tool | Description |
|------|-------------|
| `start_session` / `end_session` | Track a work session |
| `write_journal` / `read_journal` | Reflective journal (semantic/tag/project search) |

### Infrastructure
| Tool | Description |
|------|-------------|
| `get_connectivity` | Servers, containers, databases for a project |
| `list_machines` / `add_machine` | Registered machines |
| `add_container` | Register a Docker container |

### Agent specifications (v3)
| Tool | Description |
|------|-------------|
| `register_agent` / `get_agent` / `update_agent` / `list_agents` / `retire_agent` | Reusable domain-expert specs |
| `suggest_agent` | Find the relevant agent for a task |

### Specification documents (v3)
| Tool | Description |
|------|-------------|
| `create_spec` / `get_spec` / `update_spec` / `list_specs` / `retire_spec` / `search_specs` | Long-form structured project knowledge |

### MCP server registry (v3)
| Tool | Description |
|------|-------------|
| `register_mcp_server` / `get_mcp_server` / `update_mcp_server` / `retire_mcp_server` / `list_mcp_servers` | Catalog of MCP servers |
| `register_mcp_tool` / `find_mcp_tools` | Catalog & discover tools across servers |

### Annotations (v4)
| Tool | Description |
|------|-------------|
| `annotate` / `get_annotations` / `clear_annotation` | Polymorphic sticky notes on any entity |

### Consolidation (v5)
| Tool | Description |
|------|-------------|
| `list_pending_consolidations` / `approve_consolidation` / `reject_consolidation` | Human-review queue |
| `list_conflicts` / `resolve_conflict` | Contradiction handling *(resolve = admin scope)* |
| `undo_consolidation` | Reverse an applied consolidation |
| `get_consolidation_stats` | Pipeline metrics |
| `apply_backlog_batch` | Apply a historical consolidation batch |

### Admin & access (v6)
| Tool | Description |
|------|-------------|
| `check_guardrails` | Verify safety before risky operations |
| `get_permissions` | Get permissions for a scope |
| `list_clients` | List registered agent clients *(admin scope)* |

## Directory Structure

```
claude-memory/
├── db/
│   ├── schema.sql            # Base database schema
│   └── migrations/           # Versioned migrations (v4…v6)
├── docs/
│   └── plans/                # Per-version design + implementation docs
├── scripts/
│   ├── seed_data.py          # Initial data population
│   ├── issue_api_key.py      # v6: issue per-machine token (admin)
│   ├── revoke_api_key.py     # v6: revoke a token (admin)
│   ├── list_api_keys.py      # v6: list tokens (admin)
│   ├── analyze_backlog.py    # v5.1: consolidation backlog analysis
│   └── backlog_report.py     # v5.1: backlog reporting CLI
├── src/
│   ├── server.py             # MCP server entrypoint + lifespan
│   ├── auth.py               # Auth middleware
│   ├── identity.py           # v6: identity resolver (api_keys/OAuth/legacy)
│   ├── config.py · db.py · helpers.py
│   ├── consolidation/        # v5 consolidation pipeline
│   └── tools/                # 14 tool modules, 57 MCP tools
├── tests/                    # pytest suite (identity, rule-b, migration, …)
├── deploy.sh                 # rsync + container rebuild
├── docker-compose.yml · Dockerfile
├── nginx-snippet.conf
└── README.md
```

## Development

### Local Testing

```bash
# Start database only
docker-compose up -d db

# Run server locally
export DATABASE_URL="postgresql://claude:claude@localhost:5433/claude_memory"
export OPENAI_API_KEY="sk-your-key"
python -m src.server
```

### Running Tests

```bash
pip install -r requirements-dev.txt
pytest                       # full suite
pytest tests/test_identity.py tests/test_rule_b.py   # v6 attribution/enforcement
```

### Viewing Logs

```bash
# On EC2
docker logs claude_memory_mcp --tail 100 -f
docker logs claude_memory_db  --tail 100 -f
```

## Version History

| Version | Theme |
|---------|-------|
| v1 | Core memory: lessons, projects, sessions, infra, semantic search |
| v2 | Modular server, per-project CLAUDE.md storage, lesson lifecycle, project aliases |
| v3 | Codified context: agent specs, spec docs, MCP registry, `find_context` |
| v4 | Feedback loop: lesson ratings, polymorphic annotations, hybrid search |
| v5 | Consolidation pipeline: duplicate/supersede detection + auditable review queue |
| v6 | Multi-agent attribution: `source_agent`/`source_client_id`, per-machine tokens, rule-b enforcement, cross-agent consolidation skip |

> `UPDATE_NOTES.md` is a historical Jan-2026 (v1-era) deployment snapshot, kept as an artifact — it does not reflect the current tool surface; this README does. Per-version design rationale lives in `docs/plans/`.
