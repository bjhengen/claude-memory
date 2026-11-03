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

def test_cookie_roundtrip_valid(monkeypatch):
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "a" * 64)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    assert token is not None
    assert approve_gate.verify_cookie(token, now=1000.0) is True


def test_cookie_rejected_when_tampered(monkeypatch):
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "a" * 64)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    expiry, _sig = token.split(".", 1)
    forged = f"{expiry}.{'0' * 64}"
    assert approve_gate.verify_cookie(forged, now=1000.0) is False


def test_cookie_rejected_when_expired(monkeypatch):
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "a" * 64)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    assert approve_gate.verify_cookie(token, now=1000.0 + 3601) is False


def test_cookie_rejected_with_different_secret(monkeypatch):
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "a" * 64)
    token = approve_gate.make_cookie(now=1000.0, ttl=3600)
    monkeypatch.setenv("APPROVE_COOKIE_SECRET", "b" * 64)
    assert approve_gate.verify_cookie(token, now=1000.0) is False


def test_cookie_none_when_secret_unset():
    assert approve_gate.make_cookie(now=1000.0, ttl=3600) is None
    assert approve_gate.verify_cookie("1000.deadbeef", now=1000.0) is False


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
