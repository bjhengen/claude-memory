"""End-to-end identity propagation spike.

Issues a real HTTP request through FastMCP's ASGI app and confirms that
identity set in load_access_token reaches the tool handler.
"""

import asyncio
import contextlib
import contextvars
import hashlib
import os

import asyncpg
import httpx
import pytest


@contextlib.asynccontextmanager
async def _drive_lifespan(app):
    """Manually drive an ASGI app's lifespan protocol.

    httpx.ASGITransport does not dispatch lifespan events, but FastMCP's
    StreamableHTTPSessionManager initializes its anyio task group during
    Starlette's lifespan startup. Without this, /mcp returns
    "Task group is not initialized."
    """
    receive_queue: asyncio.Queue = asyncio.Queue()
    send_queue: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await receive_queue.get()

    async def send(message):
        await send_queue.put(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    task = asyncio.create_task(app(scope, receive, send))

    await receive_queue.put({"type": "lifespan.startup"})
    msg = await send_queue.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        # Best-effort: wait for shutdown.complete then let the task exit.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(send_queue.get(), timeout=5.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5.0)

# This contextvar is the simplest possible probe: any propagation failure
# between middleware and tool handler will show up as a None read.
_probe: contextvars.ContextVar[str | None] = contextvars.ContextVar("probe", default=None)


@pytest.mark.asyncio
async def test_contextvar_propagates_from_auth_to_tool():
    """Identity set in load_access_token must be readable inside tool handler."""
    test_db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://claude:claude@192.168.1.234:5434/claude_memory_test",
    )
    os.environ["DATABASE_URL"] = test_db_url
    os.environ.setdefault("OPENAI_API_KEY", "sk-dummy")
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-dummy")

    # NOTE: under the full test suite, src.server may already have been
    # imported with a different DATABASE_URL (config reads env at import time
    # and caches it as a module-level constant). We wire a pool against the
    # test DB explicitly to make this test independent of import order.
    from src import server as server_module
    from src.server import app, mcp, oauth_provider
    from src import auth as auth_module

    test_pool = await asyncpg.create_pool(test_db_url, min_size=1, max_size=2)
    oauth_provider.set_pool(test_pool)

    # Authenticate the spike request via a real api_keys row; the bearer
    # 'spike-test-key' is hash-matched by resolve_identity.
    bearer = "spike-test-key"
    key_hash = hashlib.sha256(bearer.encode()).hexdigest()
    # ON CONFLICT: a prior run that died before its finally-cleanup leaves
    # this row behind; reclaim it instead of failing forever on residue.
    apikey_row = await test_pool.fetchrow(
        """INSERT INTO api_keys (api_key_hash, family, client_name, label, scopes)
           VALUES ($1, 'claude', 'spike', 'e2e-spike', ARRAY['read','write'])
           ON CONFLICT (api_key_hash) DO UPDATE SET label = 'e2e-spike'
           RETURNING id""",
        key_hash,
    )
    # Force MCP per-session lifespan (app_lifespan -> _ensure_pool) to use
    # the same test pool, not the default-config DATABASE_URL that
    # server.py captured at import time. server.py caches both DATABASE_URL
    # and _db_pool at module level; overwriting both is necessary so that
    # both module-level reads and lazy re-creation hit the test database.
    server_module.DATABASE_URL = test_db_url
    server_module._db_pool = test_pool

    # Wrap load_access_token to set our probe.
    original = auth_module.MemoryOAuthProvider.load_access_token

    async def patched(self, token):
        _probe.set(f"saw:{token[:8]}")
        return await original(self, token)

    auth_module.MemoryOAuthProvider.load_access_token = patched

    # Register a one-shot tool that reads the probe.
    @mcp.tool(name="_spike_probe")
    async def _spike_probe() -> str:
        return _probe.get() or "MISSING"

    transport = httpx.ASGITransport(app=app)
    try:
        async with _drive_lifespan(app), httpx.AsyncClient(
            transport=transport, base_url="http://localhost:8003"
        ) as client:
            # MCP stateless_http=True still requires the initialize handshake
            # before tools/call. Do that first.
            init_resp = await client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer spike-test-key",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "spike", "version": "0.0.0"},
                    },
                },
            )
            assert init_resp.status_code == 200, init_resp.text

            resp = await client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer spike-test-key",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "_spike_probe", "arguments": {}},
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # Extract the tool's text result (FastMCP wraps it)
            result_text = body["result"]["content"][0]["text"]
            assert result_text != "MISSING", (
                "ContextVar did not propagate from load_access_token into tool handler. "
                "Plan must switch to Starlette request.state pattern (see Task 5 fallback)."
            )
            assert result_text.startswith("saw:"), result_text
            # Stronger evidence: the recorded prefix matches the bearer token
            # actually sent. Falsifies any "default value leaked through"
            # explanation for the test passing.
            assert result_text == "saw:spike-te", result_text
    finally:
        # Restore class method so we don't poison other tests in the suite.
        auth_module.MemoryOAuthProvider.load_access_token = original
        # The app lifespan shutdown closes the pool we injected into server
        # state, so clean up over a fresh connection, not the closed pool.
        conn = await asyncpg.connect(test_db_url)
        try:
            await conn.execute("DELETE FROM api_keys WHERE id = $1", apikey_row["id"])
        finally:
            await conn.close()
        # Detach the test pool from server-level state before closing it so a
        # later test that re-enters _ensure_pool() doesn't reuse a closed pool.
        server_module._db_pool = None
        await test_pool.close()
