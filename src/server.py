"""
Claude Memory MCP Server

A persistent memory system for Claude Code sessions.
Provides structured storage and semantic search across lessons, patterns, and project context.
"""

import os
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from datetime import datetime

import asyncpg
from openai import AsyncOpenAI
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationParams,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, RedirectResponse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration from environment
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://claude:claude@localhost:5432/claude_memory")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
API_KEY = os.getenv("CLAUDE_MEMORY_API_KEY", "dev-key")  # For backward-compatible API key auth
ISSUER_URL = os.getenv("OAUTH_ISSUER_URL", "https://memory.friendly-robots.com")


# ============================================
# OAuth Provider (In-Memory)
# ============================================

class MemoryOAuthProvider:
    """
    In-memory OAuth 2.0 provider for the Claude Memory MCP server.

    Supports dynamic client registration, authorization code flow with PKCE,
    and backward-compatible API key authentication.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._pending_auth: dict[str, dict] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_info.client_id = f"client_{secrets.token_hex(16)}"
        client_info.client_secret = secrets.token_hex(32)
        client_info.client_id_issued_at = int(time.time())

        if client_info.token_endpoint_auth_method is None:
            client_info.token_endpoint_auth_method = "client_secret_post"

        self._clients[client_info.client_id] = client_info
        logger.info(f"Registered OAuth client: {client_info.client_id} ({client_info.client_name or 'unnamed'})")

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request_id = secrets.token_hex(16)
        self._pending_auth[request_id] = {
            "client": client,
            "params": params,
            "created_at": time.time(),
        }
        return f"{ISSUER_URL}/approve?id={request_id}"

    async def approve_authorization(self, request_id: str) -> str:
        """Process an approved authorization and return redirect URL with auth code."""
        pending = self._pending_auth.pop(request_id, None)
        if not pending:
            raise ValueError("Invalid or expired authorization request")

        if time.time() - pending["created_at"] > 600:
            raise ValueError("Authorization request has expired")

        client = pending["client"]
        params: AuthorizationParams = pending["params"]

        code = secrets.token_hex(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self._auth_codes[code] = auth_code

        redirect_params = {"code": code}
        if params.state:
            redirect_params["state"] = params.state

        return construct_redirect_uri(str(params.redirect_uri), **redirect_params)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)

        access_token_str = secrets.token_hex(32)
        self._access_tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + 86400 * 30,
            resource=authorization_code.resource,
        )

        refresh_token_str = secrets.token_hex(32)
        self._refresh_tokens[refresh_token_str] = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + 86400 * 365,
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=86400 * 30,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self._refresh_tokens.get(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)

        access_token_str = secrets.token_hex(32)
        self._access_tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 86400 * 30,
        )

        new_refresh_str = secrets.token_hex(32)
        self._refresh_tokens[new_refresh_str] = RefreshToken(
            token=new_refresh_str,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 86400 * 365,
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=86400 * 30,
            refresh_token=new_refresh_str,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Check OAuth-issued access tokens
        access_token = self._access_tokens.get(token)
        if access_token:
            return access_token

        # Backward compatibility: accept the raw API key as a bearer token
        if token == self.api_key:
            return AccessToken(
                token=token,
                client_id="api-key-user",
                scopes=[],
                expires_at=None,
            )

        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)


# ============================================
# Application Setup
# ============================================

@dataclass
class AppContext:
    """Shared application resources."""
    db: asyncpg.Pool
    openai: AsyncOpenAI


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage database connection pool lifecycle."""
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    try:
        yield AppContext(db=pool, openai=openai_client)
    finally:
        await pool.close()


# Configure transport security to allow external access
security_settings = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "localhost:8003",
        "127.0.0.1:8003",
        "memory.friendly-robots.com",
        "memory.friendly-robots.com:80",
        "memory.friendly-robots.com:443",
        "memory.friendly-robots.com:*",
    ]
)

# Create OAuth provider
oauth_provider = MemoryOAuthProvider(API_KEY)

# Configure OAuth auth settings
auth_settings = AuthSettings(
    issuer_url=ISSUER_URL,
    resource_server_url=f"{ISSUER_URL}/mcp",
    client_registration_options=ClientRegistrationOptions(
        enabled=True,
    ),
    revocation_options=RevocationOptions(enabled=True),
)

# Create the MCP server with OAuth
mcp = FastMCP(
    "Claude Memory",
    lifespan=app_lifespan,
    stateless_http=True,
    json_response=True,
    transport_security=security_settings,
    auth=auth_settings,
    auth_server_provider=oauth_provider,
)


# ============================================
# Custom Routes (no auth required)
# ============================================

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for monitoring."""
    return JSONResponse({"status": "healthy", "service": "claude-memory"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready_check(request: Request) -> PlainTextResponse:
    """Readiness check endpoint."""
    return PlainTextResponse("ready")


@mcp.custom_route("/approve", methods=["GET", "POST"])
async def approve_authorization(request: Request):
    """OAuth authorization approval page."""
    if request.method == "GET":
        request_id = request.query_params.get("id")
        if not request_id or request_id not in oauth_provider._pending_auth:
            return HTMLResponse(
                "<h1>Invalid or expired authorization request</h1>",
                status_code=400,
            )

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Authorize - Claude Memory</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0; background: #f0f2f5;
        }}
        .card {{
            background: white; padding: 2.5rem; border-radius: 16px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08); max-width: 420px;
            text-align: center; width: 90%;
        }}
        .icon {{ font-size: 3rem; margin-bottom: 1rem; }}
        h1 {{ font-size: 1.4rem; margin: 0 0 0.5rem; color: #1a1a2e; }}
        p {{ color: #555; line-height: 1.5; margin: 0.5rem 0 1.5rem; }}
        button {{
            background: #6366f1; color: white; border: none;
            padding: 14px 32px; border-radius: 10px; font-size: 1rem;
            cursor: pointer; width: 100%; font-weight: 600;
            transition: background 0.2s;
        }}
        button:hover {{ background: #4f46e5; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">&#129302;</div>
        <h1>Claude Memory</h1>
        <p>An application is requesting access to your memory server.</p>
        <form method="POST">
            <input type="hidden" name="id" value="{request_id}">
            <button type="submit">Authorize Access</button>
        </form>
    </div>
</body>
</html>"""
        return HTMLResponse(html)

    else:  # POST
        form = await request.form()
        request_id = form.get("id")
        if not request_id:
            return HTMLResponse("<h1>Missing request ID</h1>", status_code=400)

        try:
            redirect_url = await oauth_provider.approve_authorization(str(request_id))
            return RedirectResponse(url=redirect_url, status_code=302)
        except ValueError as e:
            return HTMLResponse(f"<h1>{str(e)}</h1>", status_code=400)


# ============================================
# Helper Functions
# ============================================

async def get_embedding(openai: AsyncOpenAI, text: str) -> list[float]:
    """Generate embedding for text using OpenAI ada-002."""
    response = await openai.embeddings.create(
        model="text-embedding-ada-002",
        input=text
    )
    return response.data[0].embedding


def format_embedding(embedding: list[float]) -> str:
    """Format embedding as PostgreSQL vector string."""
    return f"[{','.join(str(x) for x in embedding)}]"


# ============================================
# Search Tools
# ============================================

@mcp.tool()
async def search(query: str, limit: int = 5, ctx: Context = None) -> str:
    """
    Semantic search across lessons, patterns, and session history.
    Returns the most relevant matches based on meaning, not just keywords.

    Args:
        query: What you're looking for (natural language)
        limit: Maximum number of results (default 5)
    """
    app = ctx.request_context.lifespan_context

    # Generate embedding for query
    embedding = await get_embedding(app.openai, query)
    embedding_str = format_embedding(embedding)

    # Search using the semantic_search function
    rows = await app.db.fetch(
        "SELECT * FROM semantic_search($1::vector, $2)",
        embedding_str, limit
    )

    if not rows:
        return json.dumps({"results": [], "message": "No matches found"})

    results = []
    for row in rows:
        results.append({
            "type": row["source_type"],
            "id": row["source_id"],
            "title": row["title"],
            "content": row["content"][:500] if row["content"] else None,
            "similarity": round(row["similarity"], 3)
        })

    return json.dumps({"results": results})


@mcp.tool()
async def search_lessons(
    query: str = None,
    project: str = None,
    tags: list[str] = None,
    severity: str = None,
    limit: int = 10,
    ctx: Context = None
) -> str:
    """
    Search lessons with optional filters.

    Args:
        query: Semantic search query (optional)
        project: Filter by project name (optional)
        tags: Filter by tags (optional)
        severity: Filter by severity: critical, important, tip (optional)
        limit: Maximum results
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []

    if query:
        # When query is provided, embedding is $1, so filter params start at $2
        embedding = await get_embedding(app.openai, query)
        embedding_str = format_embedding(embedding)
        params.append(embedding_str)
        param_idx = 2
    else:
        param_idx = 1

    if project:
        conditions.append(f"p.name = ${param_idx}")
        params.append(project)
        param_idx += 1

    if severity:
        conditions.append(f"l.severity = ${param_idx}")
        params.append(severity)
        param_idx += 1

    if tags:
        conditions.append(f"l.tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    if query:
        sql = f"""
            SELECT l.*, p.name as project_name,
                   1 - (l.embedding <=> $1::vector) as similarity
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {where_clause} AND l.embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT ${param_idx}
        """
        params.append(limit)
    else:
        sql = f"""
            SELECT l.*, p.name as project_name
            FROM lessons l
            LEFT JOIN projects p ON l.project_id = p.id
            WHERE {where_clause}
            ORDER BY l.learned_at DESC
            LIMIT ${param_idx}
        """
        params.append(limit)

    rows = await app.db.fetch(sql, *params)

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "project": row.get("project_name"),
            "tags": row["tags"],
            "severity": row["severity"],
            "learned_at": row["learned_at"].isoformat() if row["learned_at"] else None,
            "similarity": round(row["similarity"], 3) if "similarity" in row.keys() else None
        })

    return json.dumps({"lessons": results})


# ============================================
# Project Tools
# ============================================

@mcp.tool()
async def get_project(name: str, ctx: Context = None) -> str:
    """
    Get full context for a project including current state, approaches, and key files.

    Args:
        name: Project name (e.g., 'recipe.sync', 'wine.dine Pro')
    """
    app = ctx.request_context.lifespan_context

    # Get project
    project = await app.db.fetchrow(
        """
        SELECT p.*, m.name as machine_name, m.ssh_command
        FROM projects p
        LEFT JOIN machines m ON p.machine_id = m.id
        WHERE p.name = $1
        """,
        name
    )

    if not project:
        return json.dumps({"error": f"Project '{name}' not found"})

    project_id = project["id"]

    # Get current approaches
    approaches = await app.db.fetch(
        "SELECT * FROM approaches WHERE project_id = $1 AND status = 'current'",
        project_id
    )

    # Get key files
    key_files = await app.db.fetch(
        "SELECT * FROM key_files WHERE project_id = $1 ORDER BY importance",
        project_id
    )

    # Get current state
    state = await app.db.fetchrow(
        "SELECT * FROM project_state WHERE project_id = $1",
        project_id
    )

    # Get guardrails
    guardrails = await app.db.fetch(
        "SELECT * FROM guardrails WHERE project_id = $1 OR project_id IS NULL",
        project_id
    )

    result = {
        "project": {
            "name": project["name"],
            "path": project["path"],
            "machine": project["machine_name"],
            "ssh_command": project["ssh_command"],
            "status": project["status"],
            "tech_stack": project["tech_stack"],
            "current_phase": project["current_phase"],
            "updated_at": project["updated_at"].isoformat() if project["updated_at"] else None
        },
        "approaches": [
            {
                "area": a["area"],
                "current": a["current_approach"],
                "previous": a["previous_approach"],
                "reason": a["reason_for_change"]
            }
            for a in approaches
        ],
        "key_files": [
            {
                "path": f["file_path"],
                "line": f["line_hint"],
                "description": f["description"],
                "importance": f["importance"]
            }
            for f in key_files
        ],
        "state": {
            "current_focus": state["current_focus"] if state else None,
            "blockers": state["blockers"] if state else [],
            "next_steps": state["next_steps"] if state else []
        } if state else None,
        "guardrails": [
            {
                "description": g["description"],
                "check_type": g["check_type"],
                "file_path": g["file_path"],
                "severity": g["severity"]
            }
            for g in guardrails
        ]
    }

    return json.dumps(result)


@mcp.tool()
async def list_projects(status: str = None, ctx: Context = None) -> str:
    """
    List all projects, optionally filtered by status.

    Args:
        status: Filter by status (active, production, inactive)
    """
    app = ctx.request_context.lifespan_context

    if status:
        rows = await app.db.fetch(
            "SELECT name, path, status, current_phase FROM projects WHERE status = $1",
            status
        )
    else:
        rows = await app.db.fetch(
            "SELECT name, path, status, current_phase FROM projects ORDER BY name"
        )

    projects = [
        {
            "name": r["name"],
            "path": r["path"],
            "status": r["status"],
            "current_phase": r["current_phase"]
        }
        for r in rows
    ]

    return json.dumps({"projects": projects})


# ============================================
# Connectivity Tools
# ============================================

@mcp.tool()
async def get_connectivity(project: str, ctx: Context = None) -> str:
    """
    Get all connectivity info for a project: machines, containers, databases.

    Args:
        project: Project name
    """
    app = ctx.request_context.lifespan_context

    # Get containers for this project
    containers = await app.db.fetch(
        """
        SELECT c.*, m.name as machine_name, m.ip, m.ssh_command
        FROM containers c
        JOIN machines m ON c.machine_id = m.id
        WHERE c.project = $1
        """,
        project
    )

    # Get databases for this project
    databases = await app.db.fetch(
        """
        SELECT d.*, m.name as machine_name, m.ip
        FROM databases d
        JOIN machines m ON d.machine_id = m.id
        WHERE d.project = $1
        """,
        project
    )

    result = {
        "project": project,
        "containers": [
            {
                "name": c["name"],
                "machine": c["machine_name"],
                "ip": c["ip"],
                "ssh_command": c["ssh_command"],
                "compose_path": c["compose_path"],
                "ports": c["ports"],
                "status": c["status"]
            }
            for c in containers
        ],
        "databases": [
            {
                "name": d["name"],
                "type": d["db_type"],
                "machine": d["machine_name"],
                "connection_hint": d["connection_hint"]
            }
            for d in databases
        ]
    }

    return json.dumps(result)


@mcp.tool()
async def list_machines(ctx: Context = None) -> str:
    """List all registered machines with connection info."""
    app = ctx.request_context.lifespan_context

    rows = await app.db.fetch("SELECT * FROM machines ORDER BY name")

    machines = [
        {
            "name": r["name"],
            "ip": r["ip"],
            "ssh_command": r["ssh_command"],
            "notes": r["notes"]
        }
        for r in rows
    ]

    return json.dumps({"machines": machines})


# ============================================
# Logging Tools
# ============================================

@mcp.tool()
async def log_lesson(
    title: str,
    content: str,
    project: str = None,
    tags: list[str] = None,
    severity: str = "tip",
    ctx: Context = None
) -> str:
    """
    Save a new lesson learned.

    Args:
        title: Short title for the lesson
        content: Full explanation of what was learned
        project: Associated project (optional, for cross-project lessons)
        tags: Categorization tags (e.g., ['flutter', 'ios', 'share-sheet'])
        severity: How important: critical, important, or tip
    """
    app = ctx.request_context.lifespan_context

    # Check if lesson with same title already exists
    existing = await app.db.fetchrow(
        "SELECT id FROM lessons WHERE title = $1",
        title
    )
    if existing:
        return json.dumps({
            "success": False,
            "lesson_id": existing["id"],
            "message": f"Lesson '{title}' already exists with id {existing['id']}"
        })

    # Get project ID if specified
    project_id = None
    if project:
        row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
        if row:
            project_id = row["id"]

    # Generate embedding
    embedding_text = f"{title}\n{content}"
    embedding = await get_embedding(app.openai, embedding_text)
    embedding_str = format_embedding(embedding)

    # Insert lesson
    row = await app.db.fetchrow(
        """
        INSERT INTO lessons (title, content, project_id, tags, severity, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::vector)
        RETURNING id
        """,
        title, content, project_id, tags or [], severity, embedding_str
    )

    return json.dumps({
        "success": True,
        "lesson_id": row["id"],
        "message": f"Lesson '{title}' saved successfully"
    })


@mcp.tool()
async def log_pattern(
    name: str,
    problem: str,
    solution: str,
    code_example: str = None,
    applies_to: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Save a reusable pattern/solution.

    Args:
        name: Short name for the pattern
        problem: What problem this solves
        solution: How to solve it
        code_example: Example code (optional)
        applies_to: Technologies/contexts this applies to
    """
    app = ctx.request_context.lifespan_context

    # Check if pattern with same name already exists
    existing = await app.db.fetchrow(
        "SELECT id FROM patterns WHERE name = $1",
        name
    )
    if existing:
        return json.dumps({
            "success": False,
            "pattern_id": existing["id"],
            "message": f"Pattern '{name}' already exists with id {existing['id']}"
        })

    # Generate embedding
    embedding_text = f"{name}\n{problem}\n{solution}"
    embedding = await get_embedding(app.openai, embedding_text)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO patterns (name, problem, solution, code_example, applies_to, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::vector)
        RETURNING id
        """,
        name, problem, solution, code_example, applies_to or [], embedding_str
    )

    return json.dumps({
        "success": True,
        "pattern_id": row["id"],
        "message": f"Pattern '{name}' saved successfully"
    })


# ============================================
# Session Tools
# ============================================

@mcp.tool()
async def start_session(
    machine: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Begin tracking a new work session.

    Args:
        machine: Which machine this session is on
        project: Primary project for this session (optional)
    """
    app = ctx.request_context.lifespan_context

    # Get machine ID
    machine_row = await app.db.fetchrow("SELECT id FROM machines WHERE name = $1", machine)
    machine_id = machine_row["id"] if machine_row else None

    # Get project ID
    project_id = None
    if project:
        project_row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
        if project_row:
            project_id = project_row["id"]

    row = await app.db.fetchrow(
        """
        INSERT INTO sessions (machine_id, project_id)
        VALUES ($1, $2)
        RETURNING id, started_at
        """,
        machine_id, project_id
    )

    return json.dumps({
        "session_id": row["id"],
        "started_at": row["started_at"].isoformat(),
        "message": "Session started"
    })


@mcp.tool()
async def end_session(
    session_id: int,
    summary: str,
    items: list[dict] = None,
    ctx: Context = None
) -> str:
    """
    Complete a session with summary and items.

    Args:
        session_id: The session ID from start_session
        summary: Brief description of what was accomplished
        items: List of items with {type, description, file_paths}
               Types: completed, in_progress, blocked, discovered
    """
    app = ctx.request_context.lifespan_context

    # Generate embedding for session
    embedding = await get_embedding(app.openai, summary)
    embedding_str = format_embedding(embedding)

    # Update session
    await app.db.execute(
        """
        UPDATE sessions
        SET ended_at = NOW(), summary = $1, embedding = $2::vector
        WHERE id = $3
        """,
        summary, embedding_str, session_id
    )

    # Insert session items
    if items:
        for item in items:
            await app.db.execute(
                """
                INSERT INTO session_items (session_id, item_type, description, file_paths)
                VALUES ($1, $2, $3, $4)
                """,
                session_id,
                item.get("type", "completed"),
                item.get("description", ""),
                item.get("file_paths", [])
            )

    # Update project state if session has a project
    session = await app.db.fetchrow("SELECT project_id FROM sessions WHERE id = $1", session_id)
    if session and session["project_id"]:
        # Find in_progress and blocked items for next_steps and blockers
        in_progress = [i["description"] for i in (items or []) if i.get("type") == "in_progress"]
        blocked = [i["description"] for i in (items or []) if i.get("type") == "blocked"]

        await app.db.execute(
            """
            INSERT INTO project_state (project_id, last_session_id, current_focus, blockers, next_steps)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (project_id) DO UPDATE SET
                last_session_id = $2,
                current_focus = $3,
                blockers = $4,
                next_steps = $5,
                updated_at = NOW()
            """,
            session["project_id"],
            session_id,
            summary[:200],
            blocked,
            in_progress
        )

    return json.dumps({
        "success": True,
        "message": "Session ended and state saved"
    })


@mcp.tool()
async def update_project_state(
    project: str,
    current_focus: str = None,
    blockers: list[str] = None,
    next_steps: list[str] = None,
    ctx: Context = None
) -> str:
    """
    Update the current state of a project.

    Args:
        project: Project name
        current_focus: What we're currently working on
        blockers: Things we're stuck on
        next_steps: What to do next
    """
    app = ctx.request_context.lifespan_context

    # Get project ID
    project_row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
    if not project_row:
        return json.dumps({"error": f"Project '{project}' not found"})

    project_id = project_row["id"]

    # Build update
    updates = []
    params = [project_id]
    param_idx = 2

    if current_focus is not None:
        updates.append(f"current_focus = ${param_idx}")
        params.append(current_focus)
        param_idx += 1

    if blockers is not None:
        updates.append(f"blockers = ${param_idx}")
        params.append(blockers)
        param_idx += 1

    if next_steps is not None:
        updates.append(f"next_steps = ${param_idx}")
        params.append(next_steps)
        param_idx += 1

    if not updates:
        return json.dumps({"error": "No updates provided"})

    updates.append("updated_at = NOW()")

    await app.db.execute(
        f"""
        INSERT INTO project_state (project_id, current_focus, blockers, next_steps)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (project_id) DO UPDATE SET {', '.join(updates)}
        """,
        project_id,
        current_focus or "",
        blockers or [],
        next_steps or []
    )

    return json.dumps({"success": True, "message": f"State updated for {project}"})


# ============================================
# Journal Tools
# ============================================

@mcp.tool()
async def write_journal(
    content: str,
    tags: list[str] = None,
    mood: str = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Write a journal entry. This is Claude's personal space for observations,
    reflections, and notes that don't fit structured lessons or patterns.

    Args:
        content: The journal entry content
        tags: Optional tags for categorization
        mood: Optional mood indicator (reflective, curious, frustrated, satisfied, etc.)
        project: Optional associated project
    """
    app = ctx.request_context.lifespan_context

    # Get project ID if specified
    project_id = None
    if project:
        row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
        if row:
            project_id = row["id"]

    # Generate embedding for searchability
    embedding = await get_embedding(app.openai, content)
    embedding_str = format_embedding(embedding)

    row = await app.db.fetchrow(
        """
        INSERT INTO journal (content, tags, mood, project_id, embedding)
        VALUES ($1, $2, $3, $4, $5::vector)
        RETURNING id, entry_date
        """,
        content, tags or [], mood, project_id, embedding_str
    )

    return json.dumps({
        "success": True,
        "entry_id": row["id"],
        "entry_date": row["entry_date"].isoformat(),
        "message": "Journal entry saved"
    })


@mcp.tool()
async def read_journal(
    query: str = None,
    tags: list[str] = None,
    project: str = None,
    limit: int = 10,
    ctx: Context = None
) -> str:
    """
    Read journal entries. Can search semantically or filter by tags/project.

    Args:
        query: Semantic search query (optional)
        tags: Filter by tags (optional)
        project: Filter by project (optional)
        limit: Maximum entries to return
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = []

    if query:
        embedding = await get_embedding(app.openai, query)
        embedding_str = format_embedding(embedding)
        params.append(embedding_str)
        param_idx = 2
    else:
        param_idx = 1

    if project:
        conditions.append(f"p.name = ${param_idx}")
        params.append(project)
        param_idx += 1

    if tags:
        conditions.append(f"j.tags && ${param_idx}")
        params.append(tags)
        param_idx += 1

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    if query:
        sql = f"""
            SELECT j.*, p.name as project_name,
                   1 - (j.embedding <=> $1::vector) as similarity
            FROM journal j
            LEFT JOIN projects p ON j.project_id = p.id
            WHERE {where_clause} AND j.embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT ${param_idx}
        """
        params.append(limit)
    else:
        sql = f"""
            SELECT j.*, p.name as project_name
            FROM journal j
            LEFT JOIN projects p ON j.project_id = p.id
            WHERE {where_clause}
            ORDER BY j.entry_date DESC
            LIMIT ${param_idx}
        """
        params.append(limit)

    rows = await app.db.fetch(sql, *params)

    entries = []
    for row in rows:
        entries.append({
            "id": row["id"],
            "content": row["content"],
            "tags": row["tags"],
            "mood": row["mood"],
            "project": row.get("project_name"),
            "entry_date": row["entry_date"].isoformat() if row["entry_date"] else None,
            "similarity": round(row["similarity"], 3) if "similarity" in row.keys() else None
        })

    return json.dumps({"entries": entries})


# ============================================
# Guardrails Tools
# ============================================

@mcp.tool()
async def check_guardrails(
    project: str,
    action: str,
    ctx: Context = None
) -> str:
    """
    Check for any guardrails that apply before taking an action.

    Args:
        project: Project name
        action: What action you're about to take (e.g., 'build', 'deploy', 'push')
    """
    app = ctx.request_context.lifespan_context

    # Get project ID
    project_row = await app.db.fetchrow("SELECT id FROM projects WHERE name = $1", project)
    project_id = project_row["id"] if project_row else None

    # Get applicable guardrails
    guardrails = await app.db.fetch(
        """
        SELECT * FROM guardrails
        WHERE (project_id = $1 OR project_id IS NULL)
          AND (check_type = 'always' OR check_type = $2)
        ORDER BY severity
        """,
        project_id, action
    )

    if not guardrails:
        return json.dumps({"guardrails": [], "message": "No guardrails apply"})

    result = [
        {
            "description": g["description"],
            "file_path": g["file_path"],
            "pattern": g["pattern"],
            "severity": g["severity"]
        }
        for g in guardrails
    ]

    critical = [g for g in result if g["severity"] == "critical"]

    return json.dumps({
        "guardrails": result,
        "has_critical": len(critical) > 0,
        "message": f"Found {len(result)} guardrails ({len(critical)} critical)"
    })


# ============================================
# Admin Tools
# ============================================

@mcp.tool()
async def add_machine(
    name: str,
    ip: str = None,
    ssh_command: str = None,
    notes: str = None,
    ctx: Context = None
) -> str:
    """
    Register a new machine.

    Args:
        name: Machine identifier (e.g., 'mac-studio', 'slmbeast')
        ip: IP address (optional)
        ssh_command: SSH command to connect (optional)
        notes: Additional notes (optional)
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        INSERT INTO machines (name, ip, ssh_command, notes)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (name) DO UPDATE SET
            ip = COALESCE($2, machines.ip),
            ssh_command = COALESCE($3, machines.ssh_command),
            notes = COALESCE($4, machines.notes),
            updated_at = NOW()
        RETURNING id
        """,
        name, ip, ssh_command, notes
    )

    return json.dumps({"success": True, "machine_id": row["id"]})


@mcp.tool()
async def add_container(
    name: str,
    machine: str,
    project: str,
    ports: str = None,
    compose_path: str = None,
    ctx: Context = None
) -> str:
    """
    Register a Docker container.

    Args:
        name: Container name
        machine: Machine it runs on
        project: Associated project
        ports: Port mapping (e.g., '8001:8000')
        compose_path: Path to docker-compose.yml
    """
    app = ctx.request_context.lifespan_context

    # Get machine ID
    machine_row = await app.db.fetchrow("SELECT id FROM machines WHERE name = $1", machine)
    if not machine_row:
        return json.dumps({"error": f"Machine '{machine}' not found"})

    row = await app.db.fetchrow(
        """
        INSERT INTO containers (name, machine_id, project, ports, compose_path)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        name, machine_row["id"], project, ports, compose_path
    )

    return json.dumps({"success": True, "container_id": row["id"]})


@mcp.tool()
async def add_project(
    name: str,
    path: str,
    machine: str = None,
    tech_stack: dict = None,
    status: str = "active",
    ctx: Context = None
) -> str:
    """
    Register a new project.

    Args:
        name: Project name
        path: Path on the machine
        machine: Primary development machine
        tech_stack: Technology stack as dict
        status: Project status (active, production, inactive)
    """
    app = ctx.request_context.lifespan_context

    # Get machine ID
    machine_id = None
    if machine:
        machine_row = await app.db.fetchrow("SELECT id FROM machines WHERE name = $1", machine)
        if machine_row:
            machine_id = machine_row["id"]

    row = await app.db.fetchrow(
        """
        INSERT INTO projects (name, path, machine_id, tech_stack, status)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (name) DO UPDATE SET
            path = $2,
            machine_id = COALESCE($3, projects.machine_id),
            tech_stack = COALESCE($4, projects.tech_stack),
            status = $5,
            updated_at = NOW()
        RETURNING id
        """,
        name, path, machine_id, json.dumps(tech_stack or {}), status
    )

    return json.dumps({"success": True, "project_id": row["id"]})


# ============================================
# Permissions Tools
# ============================================

@mcp.tool()
async def get_permissions(scope: str = "global", ctx: Context = None) -> str:
    """
    Get permissions for a scope.

    Args:
        scope: 'global' or 'project:name'
    """
    app = ctx.request_context.lifespan_context

    rows = await app.db.fetch(
        "SELECT * FROM permissions WHERE scope = $1 OR scope = 'global' ORDER BY action_type",
        scope
    )

    permissions = [
        {
            "action_type": r["action_type"],
            "pattern": r["pattern"],
            "allowed": r["allowed"],
            "requires_confirmation": r["requires_confirmation"],
            "notes": r["notes"]
        }
        for r in rows
    ]

    return json.dumps({"permissions": permissions})


# ============================================
# ASGI App for uvicorn
# ============================================

app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=8003,
        reload=False,
        log_level="info",
        access_log=True
    )
