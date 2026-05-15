"""End-to-end tests for the admin scripts."""

import hashlib
import os
import subprocess
import sys

import pytest


def _env_with_dsn():
    env = os.environ.copy()
    env["DATABASE_URL"] = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://claude:claude@192.168.1.234:5434/claude_memory_test",
    )
    return env


@pytest.mark.asyncio
async def test_issue_api_key_creates_row(db_pool):
    result = subprocess.run(
        [sys.executable, "scripts/issue_api_key.py",
         "--family", "codex",
         "--label", "test-script-issuance",
         "--client-name", "codex-cli"],
        capture_output=True, text=True, env=_env_with_dsn(), check=True,
    )
    bearer = next(
        (line.strip() for line in result.stdout.split("\n")
         if len(line.strip()) == 64 and all(c in "0123456789abcdef" for c in line.strip())),
        None,
    )
    assert bearer, result.stdout

    h = hashlib.sha256(bearer.encode()).hexdigest()
    row = await db_pool.fetchrow(
        "SELECT family, label, client_name FROM api_keys WHERE api_key_hash = $1", h,
    )
    assert row is not None
    assert row["family"] == "codex"
    assert row["label"] == "test-script-issuance"
    assert row["client_name"] == "codex-cli"

    await db_pool.execute("DELETE FROM api_keys WHERE api_key_hash = $1", h)


@pytest.mark.asyncio
async def test_revoke_api_key_by_label(db_pool):
    raw = "revoke-test-bearer"
    h = hashlib.sha256(raw.encode()).hexdigest()
    await db_pool.execute(
        """INSERT INTO api_keys (api_key_hash, family, label)
           VALUES ($1, 'codex', 'revoke-test')""",
        h,
    )
    try:
        result = subprocess.run(
            [sys.executable, "scripts/revoke_api_key.py", "--label", "revoke-test"],
            capture_output=True, text=True, env=_env_with_dsn(), check=True,
        )
        assert "Revoked" in result.stdout
        row = await db_pool.fetchrow(
            "SELECT revoked_at FROM api_keys WHERE api_key_hash = $1", h,
        )
        assert row["revoked_at"] is not None
    finally:
        await db_pool.execute("DELETE FROM api_keys WHERE api_key_hash = $1", h)
