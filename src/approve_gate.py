"""Authentication gate for the OAuth `/approve` consent page.

The `/approve` endpoint completes an OAuth authorization: a POST there issues
an auth code that can be exchanged for a read/write access token. Without a
gate, anyone who can reach the server can register a client, drive their own
authorize flow, and self-approve — minting a token. This module requires a
passphrase at that step and, on success, issues a signed "remember this
browser" cookie so the human only types the passphrase occasionally.

Secrets are read from the environment at call time (not cached at import), so
the gate is trivially testable and a deploy that injects them takes effect on
restart:

- APPROVE_PASSPHRASE      the human secret typed on the approve page
- APPROVE_COOKIE_SECRET   high-entropy key signing the remember-me cookie

Fail closed: if APPROVE_PASSPHRASE is absent, nothing can be approved.
"""

import hashlib
import hmac
import os
import time

COOKIE_NAME = "cm_approve"
DEFAULT_TTL = 90 * 86400  # 90 days


def _passphrase() -> str | None:
    return os.environ.get("APPROVE_PASSPHRASE") or None


def _cookie_secret() -> str | None:
    return os.environ.get("APPROVE_COOKIE_SECRET") or None


def verify_passphrase(provided: str | None) -> bool:
    """True only if a passphrase is configured and `provided` matches it.

    Constant-time comparison. Fail-closed: returns False when APPROVE_PASSPHRASE
    is unset, so a misconfigured deploy cannot reopen the endpoint.
    """
    expected = _passphrase()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def make_cookie(now: float | None = None, ttl: int = DEFAULT_TTL) -> str | None:
    """Mint a signed remember-me token `<expiry>.<hex-hmac>`, or None if no secret."""
    secret = _cookie_secret()
    if not secret:
        return None
    now = time.time() if now is None else now
    expiry = str(int(now) + ttl)
    sig = hmac.new(secret.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def verify_cookie(value: str | None, now: float | None = None) -> bool:
    """True if `value` is an unexpired token signed by the current secret."""
    secret = _cookie_secret()
    if not secret or not value:
        return False
    try:
        expiry_str, sig = value.split(".", 1)
    except ValueError:
        return False
    expected_sig = hmac.new(secret.encode(), expiry_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        return int(expiry_str) > (time.time() if now is None else now)
    except ValueError:
        return False


class RateLimiter:
    """In-memory sliding-window failure counter, keyed by client IP.

    Single-process; good enough for a one-replica MCP server. Only failed
    passphrase attempts are recorded, so legitimate (cookie/correct-passphrase)
    traffic never trips it.
    """

    def __init__(self, max_attempts: int = 10, window: int = 900):
        self.max_attempts = max_attempts
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if t > now - self.window]
        self._hits[key] = hits
        return hits

    def allowed(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return len(self._recent(key, now)) < self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._recent(key, now).append(now)
