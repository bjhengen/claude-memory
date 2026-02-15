"""OAuth provider for the Claude Memory MCP server."""

import logging
import secrets
import time

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
