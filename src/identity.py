"""Identity resolver for multi-agent attribution.

Maps a request bearer to an Identity(family, client_id, scopes, source) via
one of two paths: api_keys hash or OAuth access token.

Identity is stored in a contextvars.ContextVar so tools read it via
`get_identity()` without taking it as a parameter.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    family: str             # 'claude' | 'codex' | 'unknown'
    client_id: str          # 'apikey:N' | 'oauth:<client_id>'
    scopes: list[str]       # ['read', 'write'] or includes 'admin'
    source: str             # 'apikey' | 'oauth'


_current_identity: contextvars.ContextVar[Optional[Identity]] = contextvars.ContextVar(
    "current_identity", default=None
)


def set_identity(identity: Optional[Identity]) -> contextvars.Token:
    return _current_identity.set(identity)


def get_identity() -> Optional[Identity]:
    return _current_identity.get()


def reset_identity() -> None:
    """Clear the current request's identity. Public for test isolation."""
    _current_identity.set(None)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def classify_family_from_name(client_name: Optional[str]) -> str:
    """Map an OAuth client_name (or api_keys.client_name) to a family."""
    if not client_name:
        return "unknown"
    n = client_name.lower()
    if n.startswith("claude"):
        return "claude"
    if n.startswith("codex"):
        return "codex"
    return "unknown"


async def resolve_identity(pool: asyncpg.Pool, bearer: str) -> Optional[Identity]:
    """Resolve a bearer to an Identity. Returns None if unrecognized.

    Order: api_keys -> OAuth.
    """
    # 1. api_keys hash lookup
    bearer_hash = _sha256_hex(bearer)
    row = await pool.fetchrow(
        """SELECT id, family, scopes FROM api_keys
           WHERE api_key_hash = $1 AND revoked_at IS NULL""",
        bearer_hash,
    )
    if row:
        # Touch last_seen_at. Awaited (one extra round-trip per request) for
        # simplicity; revisit if it becomes a hot-path bottleneck.
        await pool.execute(
            "UPDATE api_keys SET last_seen_at = NOW() WHERE id = $1",
            row["id"],
        )
        return Identity(
            family=row["family"],
            client_id=f"apikey:{row['id']}",
            scopes=list(row["scopes"]),
            source="apikey",
        )

    # 2. OAuth access token lookup (with expiry filter)
    row = await pool.fetchrow(
        """SELECT t.client_id, c.client_name
           FROM oauth_access_tokens t
           JOIN oauth_clients c ON c.client_id = t.client_id
           WHERE t.token = $1
             AND (t.expires_at IS NULL OR t.expires_at > $2)""",
        bearer, int(time.time()),
    )
    if row:
        oauth_client_id = row["client_id"]
        client_name = row["client_name"]

        # Touch last_seen_at (same pattern as the api_keys branch) so a
        # client/launch context going dark is detectable via get_client_health.
        await pool.execute(
            "UPDATE oauth_clients SET last_seen_at = NOW() WHERE client_id = $1",
            oauth_client_id,
        )

        family_row = await pool.fetchrow(
            "SELECT family FROM oauth_client_family WHERE client_id = $1",
            oauth_client_id,
        )
        if family_row:
            family = family_row["family"]
        else:
            family = classify_family_from_name(client_name)
            await pool.execute(
                """INSERT INTO oauth_client_family
                   (client_id, family, client_name, inferred_from)
                   VALUES ($1, $2, $3, 'client_name_prefix')
                   ON CONFLICT (client_id) DO NOTHING""",
                oauth_client_id, family, client_name,
            )
            if family == "unknown":
                logger.warning(
                    "Unknown OAuth client classified as 'unknown': "
                    "client_id=%s client_name=%r. Update oauth_client_family.family "
                    "to a known family if this is misclassified.",
                    oauth_client_id, client_name,
                )

        return Identity(
            family=family,
            client_id=f"oauth:{oauth_client_id}",
            scopes=["read", "write"],
            source="oauth",
        )

    return None


# ---------------------------------------------------------------------------
# Write-stamp + rule-b + admin
# ---------------------------------------------------------------------------

# Tables with `id` PK + source_agent where rule b applies.
OWNED_CONTENT_TABLES = frozenset({
    "lessons",
    "patterns",
    "journal",
    "agent_specs",
    "specifications",
    "mcp_servers",
    "mcp_server_tools",
    "annotations",
})

# Tables with `id` PK + source_agent where last-writer-wins.
SHARED_METADATA_TABLES = frozenset({
    "projects",
    "project_state",
    "approaches",
    "key_files",
    "guardrails",
    "permissions",
    "project_aliases",
    "machines",
    "databases",
    "containers",
    "conflicts",
    "sessions",
})


def stamp() -> tuple[str, Optional[str]]:
    """Return (source_agent, source_client_id) for the current request.

    Defaults to ('claude', None) when unauth — preserves legacy behavior for
    any code path not yet behind the resolver.
    """
    identity = get_identity()
    if identity is None:
        return ("claude", None)
    return (identity.family, identity.client_id)


async def assert_can_write(pool: asyncpg.Pool, table: str, row_id: int) -> None:
    """Raise PermissionError if the current identity cannot write to `table.row_id`."""
    if table in SHARED_METADATA_TABLES:
        return
    if table not in OWNED_CONTENT_TABLES:
        raise ValueError(
            f"assert_can_write called with unknown table '{table}'. "
            "Add it to OWNED_CONTENT_TABLES or SHARED_METADATA_TABLES."
        )

    identity = get_identity()
    current_family = identity.family if identity else "claude"
    current_scopes = identity.scopes if identity else ["read", "write"]

    if "admin" in current_scopes:
        return

    # All OWNED_CONTENT_TABLES have a SERIAL PK `id` column. Table name is
    # allow-listed above, so f-string interpolation is safe.
    row = await pool.fetchrow(
        f"SELECT source_agent FROM {table} WHERE id = $1",
        row_id,
    )
    if row is None:
        return  # caller handles missing row
    owner = row["source_agent"]
    if owner != current_family:
        raise PermissionError(
            f"agent '{current_family}' cannot modify row owned by '{owner}' in {table}"
        )


def require_admin() -> None:
    """Raise PermissionError if the current identity lacks 'admin' scope."""
    identity = get_identity()
    scopes = identity.scopes if identity else ["read", "write"]
    if "admin" not in scopes:
        family = identity.family if identity else "claude"
        raise PermissionError(
            f"agent '{family}' lacks 'admin' scope required for this operation"
        )
