"""Identity resolver for multi-agent attribution.

Maps a request bearer to an Identity(family, client_id, scopes, source) via
one of three paths: api_keys hash, OAuth access token, or legacy API_KEY env.

Identity is stored in a contextvars.ContextVar so tools read it via
`get_identity()` without taking it as a parameter.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

# Legacy API_KEY (back-compat). Module-level so tests can monkeypatch.
LEGACY_API_KEY: Optional[str] = os.getenv("CLAUDE_MEMORY_API_KEY")


@dataclass(frozen=True)
class Identity:
    family: str             # 'claude' | 'codex' | 'unknown'
    client_id: str          # 'legacy-api-key' | 'apikey:N' | 'oauth:<client_id>'
    scopes: list[str]       # ['read', 'write'] or includes 'admin'
    source: str             # 'legacy' | 'apikey' | 'oauth'


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

    Order: api_keys -> OAuth -> legacy API_KEY. (api_keys/OAuth tried first so
    a bearer that happens to match BOTH api_keys and legacy is attributed to
    api_keys for better forensics.)
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

    # 2. (Future task) OAuth token lookup
    # 3. Legacy API_KEY (back-compat)
    if LEGACY_API_KEY and bearer == LEGACY_API_KEY:
        logger.warning(
            "DEPRECATION: legacy API_KEY used as bearer. "
            "Migrate to per-machine api_keys row."
        )
        return Identity(
            family="claude",
            client_id="legacy-api-key",
            scopes=["read", "write"],
            source="legacy",
        )

    return None
