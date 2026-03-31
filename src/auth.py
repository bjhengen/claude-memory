"""OAuth provider for the Claude Memory MCP server.

Persists client registrations and tokens to PostgreSQL so they survive
server restarts. Auth codes and pending requests stay in-memory (short-lived).
"""

import json
import logging
import secrets
import time

import asyncpg
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from src.config import ISSUER_URL

logger = logging.getLogger(__name__)


class MemoryOAuthProvider:
    """
    Database-backed OAuth 2.0 provider for the Claude Memory MCP server.

    Client registrations and tokens are persisted to PostgreSQL.
    Auth codes and pending auth requests remain in-memory (they expire in minutes).
    Backward-compatible API key authentication is always available.

    Uses the shared connection pool (set via set_pool) to avoid transient
    connection failures from creating individual connections per request.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._pool: asyncpg.Pool | None = None
        # Short-lived state stays in memory
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._pending_auth: dict[str, dict] = {}

    def set_pool(self, pool: asyncpg.Pool) -> None:
        """Set the shared database connection pool. Called during app lifespan startup."""
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("OAuth provider pool not initialized — call set_pool() during startup")
        return self._pool

    # ------------------------------------------------------------------
    # Client registration
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self.pool.fetchrow(
            "SELECT raw_data FROM oauth_clients WHERE client_id = $1", client_id
        )
        if row:
            logger.info(f"Found OAuth client in DB: {client_id}")
            try:
                return OAuthClientInformationFull(**json.loads(row["raw_data"]))
            except Exception as e:
                logger.error(f"Failed to deserialize OAuth client {client_id}: {e}")
                return None
        logger.warning(f"OAuth client not found in DB: {client_id}")
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_info.client_id = f"client_{secrets.token_hex(16)}"
        client_info.client_id_issued_at = int(time.time())

        if client_info.token_endpoint_auth_method is None:
            client_info.token_endpoint_auth_method = "client_secret_post"

        # Only issue a client_secret if the client will use it for authentication.
        # Public clients (auth method "none") must NOT have a secret, otherwise
        # the SDK's ClientAuthenticator demands it even though the client won't send it.
        if client_info.token_endpoint_auth_method != "none":
            client_info.client_secret = secrets.token_hex(32)
        else:
            client_info.client_secret = None

        await self.pool.execute(
            """INSERT INTO oauth_clients (client_id, client_secret, client_name,
               token_endpoint_auth_method, client_id_issued_at, raw_data)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (client_id) DO UPDATE SET raw_data = $6""",
            client_info.client_id,
            client_info.client_secret,
            client_info.client_name,
            client_info.token_endpoint_auth_method,
            client_info.client_id_issued_at,
            client_info.model_dump_json(),
        )
        logger.info(f"Registered OAuth client: {client_info.client_id} ({client_info.client_name or 'unnamed'})")

    # ------------------------------------------------------------------
    # Authorization (short-lived, in-memory only)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Token exchange (persisted to database)
    # ------------------------------------------------------------------

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        logger.info(f"Exchanging auth code for client: {client.client_id}")
        self._auth_codes.pop(authorization_code.code, None)

        access_token_str = secrets.token_hex(32)
        access_expires = int(time.time()) + 86400 * 30

        refresh_token_str = secrets.token_hex(32)
        refresh_expires = int(time.time()) + 86400 * 365

        scopes_json = json.dumps(authorization_code.scopes)
        resource = str(authorization_code.resource) if authorization_code.resource else None

        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at, resource)
                   VALUES ($1, $2, $3::jsonb, $4, $5)""",
                access_token_str, client.client_id, scopes_json, access_expires, resource,
            )
            await conn.execute(
                """INSERT INTO oauth_refresh_tokens (token, client_id, scopes, expires_at)
                   VALUES ($1, $2, $3::jsonb, $4)""",
                refresh_token_str, client.client_id, scopes_json, refresh_expires,
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
        row = await self.pool.fetchrow(
            "SELECT client_id, scopes, expires_at FROM oauth_refresh_tokens WHERE token = $1",
            refresh_token,
        )
        if row:
            return RefreshToken(
                token=refresh_token,
                client_id=row["client_id"],
                scopes=json.loads(row["scopes"]) if row["scopes"] else [],
                expires_at=row["expires_at"],
            )
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        access_token_str = secrets.token_hex(32)
        access_expires = int(time.time()) + 86400 * 30

        new_refresh_str = secrets.token_hex(32)
        refresh_expires = int(time.time()) + 86400 * 365

        scopes_json = json.dumps(scopes)

        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM oauth_refresh_tokens WHERE token = $1", refresh_token.token
            )
            await conn.execute(
                """INSERT INTO oauth_access_tokens (token, client_id, scopes, expires_at)
                   VALUES ($1, $2, $3::jsonb, $4)""",
                access_token_str, client.client_id, scopes_json, access_expires,
            )
            await conn.execute(
                """INSERT INTO oauth_refresh_tokens (token, client_id, scopes, expires_at)
                   VALUES ($1, $2, $3::jsonb, $4)""",
                new_refresh_str, client.client_id, scopes_json, refresh_expires,
            )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=86400 * 30,
            refresh_token=new_refresh_str,
            scope=" ".join(scopes) if scopes else None,
        )

    # ------------------------------------------------------------------
    # Token validation
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        logger.info(f"load_access_token called, is_api_key={token == self.api_key}, token_prefix={token[:8]}...")
        # Backward compatibility: accept the raw API key as a bearer token
        if token == self.api_key:
            return AccessToken(
                token=token,
                client_id="api-key-user",
                scopes=[],
                expires_at=None,
            )

        # Check database for OAuth-issued access tokens
        row = await self.pool.fetchrow(
            "SELECT client_id, scopes, expires_at, resource FROM oauth_access_tokens WHERE token = $1",
            token,
        )
        if row:
            return AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=json.loads(row["scopes"]) if row["scopes"] else [],
                expires_at=row["expires_at"],
                resource=row["resource"],
            )
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            await self.pool.execute(
                "DELETE FROM oauth_access_tokens WHERE token = $1", token.token
            )
        elif isinstance(token, RefreshToken):
            await self.pool.execute(
                "DELETE FROM oauth_refresh_tokens WHERE token = $1", token.token
            )
