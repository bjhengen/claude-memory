# Claude Memory

A cross-machine, cross-project memory system for Claude Code sessions. Provides persistent storage for lessons learned, project context, infrastructure details, and session history.

## Features

- **Semantic Search**: Find relevant lessons and patterns using natural language
- **Project Context**: Store and retrieve project-specific approaches, key files, and guardrails
- **Infrastructure Tracking**: Keep track of machines, containers, and databases
- **Session History**: Log sessions and pick up where you left off
- **Cross-Project Knowledge**: Lessons learned in one project surface in others

## Architecture

- PostgreSQL 15 with pgvector for semantic search
- Python MCP server using FastMCP
- OpenAI ada-002 for embeddings
- Docker Compose deployment

## Quick Start

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env with your values:
# - POSTGRES_PASSWORD
# - OPENAI_API_KEY
# - CLAUDE_MEMORY_API_KEY
```

### 2. Deploy to AWS

```bash
./deploy.sh
```

### 3. Configure nginx (on EC2)

Add the contents of `nginx-snippet.conf` to your nginx server block, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Seed Initial Data

```bash
# Set environment variables
export DATABASE_URL="postgresql://claude:YOUR_PASSWORD@<YOUR_EC2_IP>:5433/claude_memory"
export OPENAI_API_KEY="sk-your-key"

# Run seed script
python scripts/seed_data.py
```

### 5. Configure Claude Code

On each machine, add the MCP server using the CLI:

```bash
# Add with user scope (available in all projects)
claude mcp add -s user -t http claude-memory https://your-domain.com/mcp \
  -H "Authorization: Bearer YOUR_CLAUDE_MEMORY_API_KEY"

# Verify it's connected
claude mcp list
```

**Scope options:**
- `-s user` - Available in all projects (recommended)
- `-s local` - Only available in current directory
- `-s project` - Only available in current project

The configuration is stored in `~/.claude.json`.

## MCP Tools

| Tool | Description |
|------|-------------|
| `search` | Semantic search across lessons, patterns, sessions |
| `search_lessons` | Search lessons with filters |
| `get_project` | Get full project context |
| `list_projects` | List all projects |
| `get_connectivity` | Get servers, containers, databases for a project |
| `list_machines` | List all registered machines |
| `log_lesson` | Save a new lesson learned |
| `log_pattern` | Save a reusable pattern |
| `start_session` | Begin tracking a session |
| `end_session` | Complete session with summary |
| `update_project_state` | Update project focus/blockers/next_steps |
| `check_guardrails` | Verify safety before risky operations |
| `add_machine` | Register a new machine |
| `add_container` | Register a Docker container |
| `add_project` | Register a new project |
| `get_permissions` | Get permissions for a scope |

## Directory Structure

```
claude-memory/
├── db/
│   └── schema.sql          # Database schema
├── docs/
│   └── plans/              # Design documents
├── scripts/
│   └── seed_data.py        # Initial data population
├── src/
│   └── server.py           # MCP server
├── .env.example            # Environment template
├── .gitignore
├── deploy.sh               # Deployment script
├── docker-compose.yml
├── Dockerfile
├── nginx-snippet.conf      # nginx configuration
├── README.md
└── requirements.txt
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

### Viewing Logs

```bash
# On EC2
docker logs claude_memory_mcp --tail 100 -f
docker logs claude_memory_db --tail 100 -f
```
