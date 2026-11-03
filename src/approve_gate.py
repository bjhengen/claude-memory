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

Fail closed: if APPROVE_PASSPHRASE is absent (or a known placeholder, or too
short), nothing can be approved — including the remember-me cookie path, which
is bound to the passphrase so rotating it revokes every remembered browser.
"""

import hashlib
import hmac
import os
import time

COOKIE_NAME = "cm_approve"
DEFAULT_TTL = 90 * 86400  # 90 days
_COOKIE_PURPOSE = "cm-approve-v1"  # domain-separates the cookie HMAC

# Values that must never function as a live secret (the .env.example samples and
# the common compose default). Treated as "unset" so a copy-paste deploy fails
# closed instead of shipping a guessable gate.
_PLACEHOLDERS = frozenset({
    "", "choose-a-strong-passphrase", "run-openssl-rand-hex-32", "changeme",
})
_MIN_PASSPHRASE_LEN = 8
_MIN_COOKIE_SECRET_LEN = 32


def _passphrase() -> str | None:
    v = os.environ.get("APPROVE_PASSPHRASE")
    if not v or v in _PLACEHOLDERS or len(v) < _MIN_PASSPHRASE_LEN:
        return None
    return v


def _cookie_secret() -> str | None:
    v = os.environ.get("APPROVE_COOKIE_SECRET")
    if not v or v in _PLACEHOLDERS or len(v) < _MIN_COOKIE_SECRET_LEN:
        return None
    return v


def verify_passphrase(provided: str | None) -> bool:
    """True only if a passphrase is configured and `provided` matches it.

    Constant-time comparison. Fail-closed: returns False when APPROVE_PASSPHRASE
    is unset/placeholder/too-short, so a misconfigured deploy cannot reopen the
    endpoint.
    """
    expected = _passphrase()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _cookie_sig(expiry: str) -> str | None:
    """HMAC binding the cookie to the current passphrase, so a passphrase
    rotation (or removal) invalidates every outstanding cookie. None unless both
    secrets are properly configured."""
    secret = _cookie_secret()
    passphrase = _passphrase()
    if not secret or not passphrase:
        return None
    pph_hash = hashlib.sha256(passphrase.encode()).hexdigest()
    msg = f"{_COOKIE_PURPOSE}:{pph_hash}:{expiry}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_cookie(now: float | None = None, ttl: int = DEFAULT_TTL) -> str | None:
    """Mint a signed remember-me token `<expiry>.<hex-hmac>`, or None if the
    gate is not fully configured."""
    now = time.time() if now is None else now
    expiry = str(int(now) + ttl)
    sig = _cookie_sig(expiry)
    if sig is None:
        return None
    return f"{expiry}.{sig}"


def verify_cookie(value: str | None, now: float | None = None) -> bool:
    """True if `value` is an unexpired token signed for the current passphrase."""
    if not value:
        return False
    try:
        expiry_str, sig = value.split(".", 1)
    except ValueError:
        return False
    expected_sig = _cookie_sig(expiry_str)
    if expected_sig is None or not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        return int(expiry_str) > (time.time() if now is None else now)
    except ValueError:
        return False


class RateLimiter:
    """In-memory sliding-window failure counter, keyed by client IP.

    Single-process; good enough for a one-replica MCP server. Only failed
    passphrase attempts are recorded, so legitimate (cookie/correct-passphrase)
    traffic never trips it. `allowed()` is a pure read — it never creates an
    entry — and `record_failure()` evicts keys whose hits have all aged out, so
    the map stays bounded by the number of distinct IPs failing within one
    window even under internet scanning.
    """

    def __init__(self, max_attempts: int = 10, window: int = 900):
        self.max_attempts = max_attempts
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        return [t for t in self._hits.get(key, []) if t > now - self.window]

    def allowed(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return len(self._recent(key, now)) < self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        # Prune fully-aged-out keys so the map can't grow unbounded under
        # scanning, then append this failure.
        self._hits = {
            k: recent for k, v in self._hits.items()
            if (recent := [t for t in v if t > now - self.window])
        }
        self._hits.setdefault(key, []).append(now)
