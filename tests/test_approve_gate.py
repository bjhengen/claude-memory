"""Unit tests for the /approve passphrase gate and remember-this-browser cookie.

These are pure (no DB, no HTTP). They pin the security-critical invariants:
a passphrase is required, the check is fail-closed when unconfigured, and the
remember-me cookie is a tamper-evident, expiring HMAC.
"""

import pytest

from src import approve_gate


@pytest.fixture(autouse=True)
def _clear_gate_env(monkeypatch):
    """Each test controls the gate's two secrets explicitly."""
    monkeypatch.delenv("APPROVE_PASSPHRASE", raising=False)
    monkeypatch.delenv("APPROVE_COOKIE_SECRET", raising=False)


# --- passphrase ---------------------------------------------------------

def test_verify_passphrase_accepts_correct(monkeypatch):
    monkeypatch.setenv("APPROVE_PASSPHRASE", "correct horse battery staple")
    assert approve_gate.verify_passphrase("correct horse battery staple") is True


def test_verify_passphrase_rejects_wrong(monkeypatch):
    monkeypatch.setenv("APPROVE_PASSPHRASE", "correct horse battery staple")
    assert approve_gate.verify_passphrase("wrong") is False


def test_verify_passphrase_fails_closed_when_unset():
    # No APPROVE_PASSPHRASE in the environment: nothing may be approved.
    assert approve_gate.verify_passphrase("anything") is False
    assert approve_gate.verify_passphrase("") is False


def test_verify_passphrase_rejects_empty_input(monkeypatch):
    monkeypatch.setenv("APPROVE_PASSPHRASE", "secret")
    assert approve_gate.verify_passphrase("") is False
    assert approve_gate.verify_passphrase(None) is False


# --- remember-me cookie -------------------------------------------------

def _set_secrets(monkeypatch, passphrase="correct horse battery staple", secret="a" * 64):
    monkeypatch.setenv("APPROVE_PASSPHRASE", passphrase)
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", secret)


def test_cookie_roundtrip_valid(monkeypatch):
    _set_secrets(monkeypatch)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    assert token is not None
    assert approve_gate.verify_cookie(token, now=1000.0) is True


def test_cookie_rejected_when_tampered(monkeypatch):
    _set_secrets(monkeypatch)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    expiry, _sig = token.split(".", 1)
    forged = f"{expiry}.{'0' * 64}"
    assert approve_gate.verify_cookie(forged, now=1000.0) is False


def test_cookie_rejected_when_expired(monkeypatch):
    _set_secrets(monkeypatch)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    assert approve_gate.verify_cookie(token, now=1000.0 + 3601) is False


def test_cookie_rejected_with_different_secret(monkeypatch):
    _set_secrets(monkeypatch)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "b" * 64)
    assert approve_gate.verify_cookie(token, now=1000.0) is False


def test_cookie_rejected_when_passphrase_rotated(monkeypatch):
    """Finding 2: rotating the passphrase must invalidate remembered browsers."""
    _set_secrets(monkeypatch)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    monkeypatch.setenv("APPROVE_PASSPHRASE", "a-new-passphrase-after-rotation")
    assert approve_gate.verify_cookie(token, now=1000.0) is False


def test_cookie_fails_closed_when_passphrase_unset(monkeypatch):
    """Finding 2: the cookie path must also fail closed if the passphrase is gone."""
    _set_secrets(monkeypatch)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    monkeypatch.delenv("APPROVE_PASSPHRASE")
    assert approve_gate.verify_cookie(token, now=1000.0) is False


def test_cookie_none_when_secret_unset(monkeypatch):
    monkeypatch.setenv("APPROVE_PASSPHRASE", "secret")
    assert approve_gate.make_cookie(now=1000.0, ttl=3600) is None
    assert approve_gate.verify_cookie("1000.deadbeef", now=1000.0) is False


# --- placeholder / weak-secret rejection (Finding 3) --------------------

def test_placeholder_passphrase_treated_as_unset(monkeypatch):
    monkeypatch.setenv("APPROVE_PASSPHRASE", "choose-a-strong-passphrase")
    assert approve_gate.verify_passphrase("choose-a-strong-passphrase") is False


def test_short_or_placeholder_cookie_secret_treated_as_unset(monkeypatch):
    monkeypatch.setenv("APPROVE_PASSPHRASE", "a-real-passphrase")
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "too-short")
    assert approve_gate.make_cookie(now=1000.0, ttl=3600) is None
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "run-openssl-rand-hex-32")
    assert approve_gate.make_cookie(now=1000.0, ttl=3600) is None


# --- rate limiter -------------------------------------------------------

def test_rate_limiter_blocks_after_max_failures():
    rl = approve_gate.RateLimiter(max_attempts=3, window=900)
    assert rl.allowed("1.2.3.4", now=0.0) is True
    for i in range(3):
        rl.record_failure("1.2.3.4", now=float(i))
    assert rl.allowed("1.2.3.4", now=3.0) is False


def test_rate_limiter_allows_after_window(monkeypatch):
    rl = approve_gate.RateLimiter(max_attempts=3, window=900)
    for i in range(3):
        rl.record_failure("1.2.3.4", now=float(i))
    assert rl.allowed("1.2.3.4", now=3.0) is False
    # After the window passes, the old failures no longer count.
    assert rl.allowed("1.2.3.4", now=1000.0) is True


def test_rate_limiter_allowed_does_not_retain_keys():
    """A read must not create a permanent entry — else every scanning IP leaks."""
    rl = approve_gate.RateLimiter(max_attempts=3, window=900)
    rl.allowed("transient-ip", now=0.0)
    assert "transient-ip" not in rl._hits


def test_rate_limiter_evicts_aged_out_keys():
    """record_failure must drop keys whose failures have all aged out."""
    rl = approve_gate.RateLimiter(max_attempts=3, window=10)
    rl.record_failure("old-ip", now=0.0)
    assert "old-ip" in rl._hits
    rl.record_failure("new-ip", now=100.0)  # old-ip's single hit is now stale
    assert "old-ip" not in rl._hits
