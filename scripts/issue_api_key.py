#!/usr/bin/env python3
"""Issue a new API key.

Usage:
    python scripts/issue_api_key.py --family codex --label "My Codex laptop" \
        [--client-name codex-cli] [--scopes read write]

Prints the raw bearer once to stdout. DB stores only the sha256 hash.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg


async def main(args):
    raw = secrets.token_hex(32)
    h = hashlib.sha256(raw.encode()).hexdigest()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            """INSERT INTO api_keys
               (api_key_hash, family, client_name, label, scopes)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            h, args.family, args.client_name, args.label, args.scopes,
        )
    finally:
        await conn.close()

    print(f"Issued API key for family='{args.family}' label='{args.label}'")
    print(f"   id: {row['id']}")
    print(f"   client_name: {args.client_name}")
    print(f"   scopes: {', '.join(args.scopes)}")
    print()
    print("Bearer token (store NOW -- will not be shown again):")
    print(f"   {raw}")
    print()
    suggested = "CODEX_MEMORY_TOKEN" if args.family == "codex" else "CLAUDE_MEMORY_TOKEN"
    print(f"Suggested env var name: {suggested}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--family", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--client-name", default=None)
    p.add_argument("--scopes", nargs="+", default=["read", "write"])
    sys.exit(asyncio.run(main(p.parse_args())))
