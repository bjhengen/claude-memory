"""
Claude Memory MCP Server

A persistent memory system for Claude Code sessions.
Provides structured storage and semantic search across lessons, patterns, and project context.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import asyncpg
from openai import AsyncOpenAI
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, RedirectResponse

from src.config import DATABASE_URL, OPENAI_API_KEY, API_KEY, ISSUER_URL, security_settings
from src.auth import MemoryOAuthProvider

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================
# Application Setup
# ============================================

@dataclass
class AppContext:
    """Shared application resources."""
    db: asyncpg.Pool
    openai: AsyncOpenAI


# App-level shared resources (outlive individual MCP sessions)
_db_pool: asyncpg.Pool | None = None
_openai_client: AsyncOpenAI | None = None


async def _ensure_pool() -> tuple[asyncpg.Pool, AsyncOpenAI]:
    """Create or return the shared connection pool and OpenAI client.

    The pool lives at the ASGI app level, NOT per-MCP-session.
    With stateless_http=True the MCP lifespan runs per-request;
    creating/closing the pool there left OAuth routes (which run
    outside MCP sessions) hitting a dead pool.
    """
    global _db_pool, _openai_client
    if _db_pool is None or _db_pool._closed:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        oauth_provider.set_pool(_db_pool)
        logger.info("Database connection pool created")
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _db_pool, _openai_client


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Provide shared resources to MCP tool handlers.

    Pool is managed at ASGI app level (_ensure_pool / shutdown event),
    NOT created or closed here — this runs per-session in stateless mode.
    """
    pool, openai_client = await _ensure_pool()
    yield AppContext(db=pool, openai=openai_client)


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
# Register tool modules (must be after mcp is created)
# ============================================

import src.tools.search      # noqa: E402, F401
import src.tools.projects    # noqa: E402, F401
import src.tools.infra       # noqa: E402, F401
import src.tools.lessons     # noqa: E402, F401
import src.tools.sessions    # noqa: E402, F401
import src.tools.journal     # noqa: E402, F401
import src.tools.admin       # noqa: E402, F401
import src.tools.agents      # noqa: E402, F401
import src.tools.specs       # noqa: E402, F401
import src.tools.mcp_registry  # noqa: E402, F401
import src.tools.annotations  # noqa: E402, F401


# ============================================
# ASGI App for uvicorn
# ============================================

app = mcp.streamable_http_app()


@app.on_event("startup")
async def startup():
    """Create the shared DB pool at ASGI app startup (before any requests)."""
    await _ensure_pool()
    logger.info("ASGI app startup complete — pool ready for OAuth and MCP")


@app.on_event("shutdown")
async def shutdown():
    """Close the shared DB pool on ASGI app shutdown."""
    global _db_pool
    if _db_pool is not None:
        await _db_pool.close()
        _db_pool = None
        logger.info("Database connection pool closed")


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
