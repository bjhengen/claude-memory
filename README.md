# Claude Memory

**A persistent, cross-agent memory server for AI coding sessions.**

One shared knowledge base — lessons learned, project context, infrastructure topology, reusable agent/spec definitions, and a reflective journal — that every machine and every coding agent (Claude, Codex, …) reads from and writes to over the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), with per-write attribution so contributions never silently collide.

---

## Why this exists

AI coding agents start every session from zero. Context that took real effort to discover — a deployment gotcha, why a library was chosen, the shape of an unfamiliar subsystem — evaporates when the session ends, and the next session (often on a different machine, increasingly with a *different agent*) rediscovers it from scratch.

Claude Memory is a long-lived MCP server that gives agents a shared, queryable memory across machines, projects, **and agent families**. It is deliberately *not* a generic vector store: it models the structure of developer knowledge — lessons, patterns, project state, infrastructure, codified agent specs — and adds the machinery to keep that memory trustworthy as it grows and as multiple agents write to it concurrently.

It has run in production as the author's daily driver since early 2026, accumulating ~750 lessons across several machines and, as of v6, more than one agent family.

## What makes it interesting

- **Cross-agent attribution (v6).** Every write is stamped with its `source_agent` (e.g. `claude`, `codex`) and client identity. *Owned* content (lessons, journal, specs) is protected by an ownership rule — one agent can't silently overwrite another's memory — while *shared* project metadata is last-writer-wins. This is what lets Claude and Codex collaborate in one corpus without drift.
- **Self-consolidating memory (v5).** Each new lesson is embedding-gated and adjudicated by an LLM judge against its nearest neighbors. High-confidence duplicates/supersedes merge automatically; everything ambiguous lands in an *auditable human-review queue*. Cross-agent pairs are deliberately never auto-merged.
- **Tiered, codified context (v3).** Beyond ad-hoc lessons, the server stores reusable **agent specifications**, long-form **spec documents**, and a **registry of MCP servers/tools** — all retrievable through a single `find_context` call.
- **Hybrid retrieval with feedback (v4).** Semantic + keyword search with confidence-weighted ranking; up/down lesson ratings feed back into result ordering, and polymorphic annotations can be attached to any entity.

## How a request flows

```
  bearer token
       │
       ▼
 ┌─────────────┐   api_keys (per-machine/per-agent hashed token)
 │  identity   │── or ─────────────────────────────────────────►  Identity
 │  resolver   │   OAuth access token                              (family, client, scopes)
 └─────────────┘
       │ every write stamped with source_agent + source_client_id
       ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  owned content  ── rule-b ownership check                     │
 │  shared metadata ── last-writer-wins                          │
 └──────────────────────────────────────────────────────────────┘
       │ new lessons
       ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  consolidation: embedding gate → LLM judge → auto-merge       │
 │  or human-review queue   (cross-agent pairs never auto-merge) │
 └──────────────────────────────────────────────────────────────┘
```

## Architecture

- **PostgreSQL 16** (`pgvector/pgvector:pg16`) for structured storage + semantic search
- **Python 3.11** MCP server built on **FastMCP**, served over HTTP
- **OpenAI `text-embedding-ada-002`** for embeddings
- **Docker Compose** behind nginx
- **58 MCP tools** across 14 functional modules

### Multi-agent identity (v6)

Authentication resolves a caller to an identity in this order:

1. **`api_keys` table** — per-machine/per-agent bearer tokens, hash-matched; identifies the calling agent via `family` / `client_name` / `label`. Issued/revoked/listed with the admin scripts in `scripts/` (per-machine revocation, no shared secret to rotate).
2. **OAuth access tokens** (with expiry filtering), for clients that register dynamically.

Every write tool stamps `source_agent` (e.g. `claude`, `codex`) and `source_client_id` onto the row. Shared-metadata writes are last-writer-wins; owned-content updates/retires enforce **rule-b** (an agent may only mutate its own content unless it holds `admin` scope). Cross-agent pairs are skipped by the consolidation pipeline, so two agents writing in different voices on the same topic are never silently merged.

## The 58 tools

Grouped by surface (full table below):

| Area | Tools |
|------|-------|
| **Search & retrieval** | `search`, `search_lessons`, `find_context` |
| **Lessons & patterns** | `log_lesson`, `log_pattern`, `update_lesson`, `retire_lesson`, `rate_lesson` |
| **Projects** | `get_project`, `list_projects`, `add_project`, `update_project_state`, per-project `CLAUDE.md` storage, `merge_projects` |
| **Sessions & journal** | `start_session`/`end_session`, `write_journal`/`read_journal` |
| **Infrastructure** | `get_connectivity`, `list_machines`/`add_machine`, `add_container` |
| **Codified context (v3)** | agent specs, spec documents, MCP server/tool registry, `suggest_agent` |
| **Feedback (v4)** | `annotate`/`get_annotations`/`clear_annotation` |
| **Consolidation (v5)** | review queue, conflict handling, `undo_consolidation`, `get_consolidation_stats` |
| **Admin & access (v6)** | `check_guardrails`, `get_permissions`, `list_clients`, `get_client_health` |

<details>
<summary><b>Full tool reference (all 58)</b></summary>

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
| `get_client_health` | Per-client last-seen / staleness view (no admin needed) |

</details>

## Quick start

### 1. Configure environment

```bash
cp .env.example .env
# Fill in:
#   POSTGRES_PASSWORD
#   OPENAI_API_KEY        (embeddings)
#   ANTHROPIC_API_KEY     (consolidation judge)
#   PUBLIC_HOST           (prod only — the host you serve under, e.g. memory.example.com)
```

### 2. Run locally

```bash
docker-compose up -d db                 # Postgres + pgvector
export DATABASE_URL="postgresql://claude:claude@localhost:5433/claude_memory"
export OPENAI_API_KEY="sk-your-key"
python -m src.server                    # MCP server on :8003
```

### 3. Deploy

```bash
EC2_HOST=ubuntu@your-host SSH_KEY=~/.ssh/your-key.pem ./deploy.sh
```

`deploy.sh` syncs the project and rebuilds the containers. In production the server sits behind nginx, and `PUBLIC_HOST` is added to the transport-security (DNS-rebinding) allowlist — see `nginx-snippet.conf`. The base schema is `db/schema.sql`; versioned migrations live in `db/migrations/` and apply in order.

### 4. Issue a per-machine token and connect a client

```bash
# On the server, against the prod DB:
python scripts/issue_api_key.py --label "Workstation" --client-name claude-code --family claude

# On each machine (Codex uses its analogous MCP config):
claude mcp add -s user -t http claude-memory https://your-domain.com/mcp \
  -H "Authorization: Bearer YOUR_PER_MACHINE_TOKEN"
claude mcp list
```

## Repository layout

```
claude-memory/
├── db/            schema.sql + versioned migrations (v4…v6)
├── docs/plans/    per-version design + implementation docs
├── scripts/       seeding, per-machine token admin, backlog analysis
├── src/
│   ├── server.py        MCP entrypoint + lifespan
│   ├── identity.py      v6 identity resolver (api_keys / OAuth)
│   ├── auth.py          OAuth provider + token validation
│   ├── consolidation/   v5 consolidation pipeline
│   └── tools/           14 tool modules, 58 MCP tools
├── tests/         pytest suite (identity, rule-b, migration, …)
├── deploy.sh · docker-compose.yml · Dockerfile · nginx-snippet.conf
```

## Development

```bash
pip install -r requirements-dev.txt
pytest                                   # full suite
pytest tests/test_identity.py tests/test_rule_b.py   # v6 attribution/enforcement
```

Set `TEST_DATABASE_URL` to point the suite at a Postgres test database with the current schema applied.

## Version history

| Version | Theme |
|---------|-------|
| v1 | Core memory: lessons, projects, sessions, infra, semantic search |
| v2 | Modular server, per-project CLAUDE.md storage, lesson lifecycle, project aliases |
| v3 | Codified context: agent specs, spec docs, MCP registry, `find_context` |
| v4 | Feedback loop: lesson ratings, polymorphic annotations, hybrid search |
| v5 | Consolidation pipeline: duplicate/supersede detection + auditable review queue |
| v6 | Multi-agent attribution: `source_agent`/`source_client_id`, per-machine tokens, rule-b enforcement, cross-agent consolidation skip |

Per-version design rationale lives in `docs/plans/`.
