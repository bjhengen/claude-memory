#!/usr/bin/env python3
"""
Log the current session and add claude-memory project to the database.
"""

import os
import json
import asyncio
import asyncpg
from openai import AsyncOpenAI

# Connect via SSH tunnel (run: ssh -L 5433:localhost:5433 -i ~/.ssh/<YOUR_KEY>.pem ubuntu@<YOUR_EC2_IP>)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://claude:claude_memory_secret@localhost:5433/claude_memory"
)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


async def get_embedding(client: AsyncOpenAI, text: str) -> list[float]:
    """Generate embedding for text."""
    response = await client.embeddings.create(
        model="text-embedding-ada-002",
        input=text[:8000]
    )
    return response.data[0].embedding


def format_embedding(embedding: list[float]) -> str:
    """Format embedding as PostgreSQL vector string."""
    return f"[{','.join(str(x) for x in embedding)}]"


async def add_claude_memory_project(pool: asyncpg.Pool):
    """Add the claude-memory project."""
    mac_studio = await pool.fetchrow("SELECT id FROM machines WHERE name = 'mac-studio'")
    aws = await pool.fetchrow("SELECT id FROM machines WHERE name = 'aws-ec2'")

    # Add the project
    await pool.execute(
        """
        INSERT INTO projects (name, path, machine_id, status, tech_stack, current_phase)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (name) DO UPDATE SET
            path = $2,
            machine_id = $3,
            status = $4,
            tech_stack = $5,
            current_phase = $6,
            updated_at = NOW()
        """,
        "claude-memory",
        "~/dev/claude-memory",
        mac_studio["id"],
        "production",
        json.dumps({
            "backend": "FastAPI + MCP SDK",
            "database": "PostgreSQL + pgvector",
            "embeddings": "OpenAI ada-002",
            "hosting": "Docker on AWS EC2"
        }),
        "Initial deployment complete"
    )
    print("Added claude-memory project")

    # Add the container
    await pool.execute(
        """
        INSERT INTO containers (name, machine_id, project, ports, compose_path)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT DO NOTHING
        """,
        "claude-memory",
        aws["id"],
        "claude-memory",
        "8004:8003",
        "~/claude-memory/docker-compose.yml"
    )
    print("Added claude-memory container")

    # Add key files
    project = await pool.fetchrow("SELECT id FROM projects WHERE name = 'claude-memory'")

    key_files = [
        {
            "file_path": "src/server.py",
            "line_hint": None,
            "description": "MCP server with 16 tools for memory operations",
            "importance": "critical"
        },
        {
            "file_path": "db/schema.sql",
            "line_hint": None,
            "description": "PostgreSQL schema with pgvector for semantic search",
            "importance": "important"
        },
        {
            "file_path": "docker-compose.yml",
            "line_hint": None,
            "description": "Docker deployment configuration",
            "importance": "important"
        }
    ]

    for f in key_files:
        await pool.execute(
            """
            INSERT INTO key_files (project_id, file_path, line_hint, description, importance)
            VALUES ($1, $2, $3, $4, $5)
            """,
            project["id"], f["file_path"], f["line_hint"], f["description"], f["importance"]
        )
    print(f"Added {len(key_files)} key files for claude-memory")


async def add_session_lessons(pool: asyncpg.Pool, openai: AsyncOpenAI):
    """Add lessons learned from this session."""
    lessons = [
        {
            "title": "MCP SDK uses Starlette for HTTP transport",
            "content": "The FastMCP SDK doesn't support a 'port' parameter in mcp.run(). For HTTP transport, use uvicorn with Starlette wrapper: app = Starlette(routes=[Mount('/', mcp.streamable_http_app())]) and run with uvicorn src.server:app",
            "project_id": None,  # Cross-project
            "tags": ["mcp", "fastmcp", "python", "uvicorn", "starlette"],
            "severity": "important"
        },
        {
            "title": "MCP requires TransportSecuritySettings for external access",
            "content": "When exposing an MCP server externally, you must configure TransportSecuritySettings with allowed_hosts to prevent 'Invalid Host header' errors. Include the domain with and without ports, and wildcards if needed.",
            "project_id": None,
            "tags": ["mcp", "security", "deployment", "nginx"],
            "severity": "important"
        },
        {
            "title": "asyncpg requires json.dumps for JSONB fields",
            "content": "When inserting Python dicts into PostgreSQL JSONB columns via asyncpg, you must use json.dumps() to serialize them first. asyncpg expects strings, not dict objects.",
            "project_id": None,
            "tags": ["python", "asyncpg", "postgresql", "jsonb"],
            "severity": "tip"
        }
    ]

    # Get claude-memory project id
    project = await pool.fetchrow("SELECT id FROM projects WHERE name = 'claude-memory'")

    for lesson in lessons:
        embed_text = f"{lesson['title']}\n{lesson['content']}"
        embedding = await get_embedding(openai, embed_text)
        embedding_str = format_embedding(embedding)

        await pool.execute(
            """
            INSERT INTO lessons (title, content, project_id, tags, severity, embedding)
            VALUES ($1, $2, $3, $4, $5, $6::vector)
            ON CONFLICT DO NOTHING
            """,
            lesson["title"], lesson["content"], lesson["project_id"],
            lesson["tags"], lesson["severity"], embedding_str
        )

    print(f"Added {len(lessons)} lessons with embeddings")


async def log_session(pool: asyncpg.Pool):
    """Log this session."""
    mac_studio = await pool.fetchrow("SELECT id FROM machines WHERE name = 'mac-studio'")
    project = await pool.fetchrow("SELECT id FROM projects WHERE name = 'claude-memory'")

    # Create session
    session = await pool.fetchrow(
        """
        INSERT INTO sessions (machine_id, project_id, summary)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        mac_studio["id"],
        project["id"],
        "Built and deployed claude-memory MCP server - a cross-machine, cross-project memory system for Claude Code sessions"
    )

    # Add session items (schema uses 'description' column, not 'content')
    items = [
        {
            "item_type": "completed",
            "description": "PostgreSQL + pgvector for hybrid structured and semantic search - enables both precise queries and fuzzy matching via vector similarity"
        },
        {
            "item_type": "completed",
            "description": "OpenAI ada-002 for embeddings - 1536 dimensions, good balance of quality and cost"
        },
        {
            "item_type": "completed",
            "description": "MCP server with FastMCP + Starlette + uvicorn - provides HTTP transport for cross-machine access"
        },
        {
            "item_type": "completed",
            "description": "Docker Compose deployment on AWS EC2 - isolated from other workloads, easy to manage"
        },
        {
            "item_type": "completed",
            "description": "Created comprehensive PostgreSQL schema with 14 tables (machines, projects, lessons, patterns, sessions, etc.)"
        },
        {
            "item_type": "completed",
            "description": "Implemented MCP server with 16 tools (search, log_lesson, start_session, get_project, etc.)"
        },
        {
            "item_type": "completed",
            "description": "Deployed to AWS EC2 at memory.friendly-robots.com (port 8004 externally, nginx reverse proxy)"
        },
        {
            "item_type": "completed",
            "description": "Added MCP server to Claude Code configuration in ~/.claude.json under mcpServers"
        },
        {
            "item_type": "discovered",
            "description": "MCP SDK port parameter doesn't work with mcp.run() - fixed by using uvicorn directly with Starlette app wrapper"
        },
        {
            "item_type": "discovered",
            "description": "Host validation blocking external access - fixed with TransportSecuritySettings allowed_hosts"
        }
    ]

    for item in items:
        await pool.execute(
            """
            INSERT INTO session_items (session_id, item_type, description)
            VALUES ($1, $2, $3)
            """,
            session["id"], item["item_type"], item["description"]
        )

    print(f"Logged session with {len(items)} items")


async def add_mcp_pattern(pool: asyncpg.Pool, openai: AsyncOpenAI):
    """Add MCP server deployment pattern."""
    pattern = {
        "name": "MCP HTTP Server Deployment Pattern",
        "problem": "Need to deploy an MCP server accessible over HTTP from multiple machines",
        "solution": "Use FastMCP with Starlette wrapper, uvicorn for serving, TransportSecuritySettings for host validation, and nginx as reverse proxy",
        "code_example": """# server.py
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

security_settings = TransportSecuritySettings(
    allowed_hosts=["localhost:8003", "your-domain.com", "your-domain.com:*"]
)

mcp = FastMCP("Server Name", transport_security=security_settings, stateless_http=True)

# Define tools...

app = Starlette(routes=[Mount("/", mcp.streamable_http_app())])

# Run with: uvicorn src.server:app --host 0.0.0.0 --port 8003

# nginx config:
# location /mcp { proxy_pass http://127.0.0.1:8004; }
""",
        "applies_to": ["mcp", "python", "fastmcp", "deployment"]
    }

    embed_text = f"{pattern['name']}\n{pattern['problem']}\n{pattern['solution']}"
    embedding = await get_embedding(openai, embed_text)
    embedding_str = format_embedding(embedding)

    await pool.execute(
        """
        INSERT INTO patterns (name, problem, solution, code_example, applies_to, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::vector)
        ON CONFLICT DO NOTHING
        """,
        pattern["name"], pattern["problem"], pattern["solution"],
        pattern["code_example"], pattern["applies_to"], embedding_str
    )

    print("Added MCP deployment pattern")


async def main():
    """Main function."""
    print("Connecting to database...")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    openai = AsyncOpenAI(api_key=OPENAI_API_KEY)

    try:
        print("\nLogging session data...\n")

        await add_claude_memory_project(pool)
        await add_session_lessons(pool, openai)
        await add_mcp_pattern(pool, openai)
        await log_session(pool)

        print("\n=== Session logged successfully ===")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
