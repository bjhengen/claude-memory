#!/usr/bin/env python3
"""List api_keys with status."""

import argparse
import asyncio
import os
import sys

import asyncpg


async def main(args):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        where = "" if args.include_revoked else "WHERE revoked_at IS NULL"
        rows = await conn.fetch(
            f"""SELECT id, family, client_name, label, scopes, created_at,
                       last_seen_at, revoked_at
                FROM api_keys {where} ORDER BY id"""
        )
    finally:
        await conn.close()

    if not rows:
        print("No keys.")
        return 0

    print(f"{'id':<4} {'family':<8} {'label':<40} {'last_seen':<20} {'status'}")
    print("-" * 100)
    for r in rows:
        status = "REVOKED" if r["revoked_at"] else "active"
        last = r["last_seen_at"].isoformat(timespec="seconds") if r["last_seen_at"] else "never"
        print(f"{r['id']:<4} {r['family']:<8} {(r['label'] or ''):<40} {last:<20} {status}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--include-revoked", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
