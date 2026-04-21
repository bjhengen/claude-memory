#!/usr/bin/env python3
"""
v5.1 backlog analyzer — one-shot pass over every above-threshold lesson pair.

Connects to the DB via DATABASE_URL, enumerates all live-live pairs with
cosine >= threshold, judges each via Claude Haiku, and writes results to
the `backlog_analysis` table. Resumable by re-invoking with the same batch-id.

Usage:
    python -m scripts.analyze_backlog --batch-id pilot-YYYY-MM-DD [options]

Options:
    --cosine-threshold  float   Minimum pairwise cosine (default 0.85)
    --concurrency       int     Max in-flight Anthropic calls (default 10)
    --limit             int     Process only the first N remaining pairs
    --dry-run                   Count pairs and print plan; no Anthropic calls
"""

import argparse
import asyncio
import logging
import os
import sys

import asyncpg
from anthropic import AsyncAnthropic

from src.consolidation import config
from src.consolidation.backlog import generate_pairs, judge_and_record

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("analyze_backlog")


async def _already_judged_pairs(pool, batch_run_id):
    rows = await pool.fetch(
        "SELECT lesson_a_id, lesson_b_id FROM backlog_analysis WHERE batch_run_id=$1",
        batch_run_id,
    )
    return {(r["lesson_a_id"], r["lesson_b_id"]) for r in rows}


async def _worker(sem, pool, anthropic, pair, batch_run_id, model, timeout, counter, total):
    async with sem:
        await judge_and_record(pool, anthropic, pair, batch_run_id, model, timeout)
        counter["done"] += 1
        if counter["done"] % 25 == 0:
            logger.info(
                "[%d/%d] pair #%s↔#%s cosine=%.3f",
                counter["done"], total,
                pair["lesson_a_id"], pair["lesson_b_id"], pair["cosine"],
            )


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", required=True)
    p.add_argument("--cosine-threshold", type=float, default=0.85)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL env var is required")
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY") and not args.dry_run:
        logger.error("ANTHROPIC_API_KEY env var is required (unless --dry-run)")
        return 2

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=5)
    try:
        pairs = await generate_pairs(pool, cosine_threshold=args.cosine_threshold)
        total_all = len(pairs)
        logger.info(
            "total pairs above cosine %.2f: %d", args.cosine_threshold, total_all,
        )

        already = await _already_judged_pairs(pool, args.batch_id)
        remaining = [
            pr for pr in pairs
            if (pr["lesson_a_id"], pr["lesson_b_id"]) not in already
        ]
        logger.info(
            "resume state: %d already judged, %d remaining for batch %r",
            len(already), len(remaining), args.batch_id,
        )

        if args.limit is not None:
            remaining = remaining[: args.limit]
            logger.info("--limit %d active; processing first %d", args.limit, len(remaining))

        if args.dry_run:
            logger.info("DRY RUN — no Anthropic calls will be made; exiting.")
            return 0

        if not remaining:
            logger.info("nothing to do; batch is complete.")
            return 0

        anthropic = AsyncAnthropic()
        sem = asyncio.Semaphore(args.concurrency)
        counter = {"done": 0}

        tasks = [
            asyncio.create_task(_worker(
                sem, pool, anthropic, pr, args.batch_id,
                config.JUDGE_MODEL, config.JUDGE_TIMEOUT_SECONDS,
                counter, len(remaining),
            ))
            for pr in remaining
        ]
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            logger.warning("interrupted — waiting for in-flight pairs to settle")
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("complete — %d pairs processed this run", counter["done"])
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
