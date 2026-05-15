#!/usr/bin/env python3
"""Revoke an API key by id or label."""

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
    if not (args.id or args.label):
        print("Provide --id or --label.", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        if args.id:
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE id = $1 AND revoked_at IS NULL", args.id,
            )
        else:
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE label = $1 AND revoked_at IS NULL", args.label,
            )
    finally:
        await conn.close()

    affected = int(result.split()[-1])
    if affected == 0:
        print("No matching active key.", file=sys.stderr)
        return 3
    print(f"Revoked {affected} key(s).")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--id", type=int)
    p.add_argument("--label")
    sys.exit(asyncio.run(main(p.parse_args())))
