#!/usr/bin/env python3
"""
v5.1 backlog report — render the backlog_analysis table as markdown or JSON.

Usage:
    python -m scripts.backlog_report --batch-id pilot-YYYY-MM-DD [options]

Options:
    --format  {markdown,json,both}   Output format (default markdown)
    --output  PATH                    Write to file instead of stdout
"""

import argparse
import asyncio
import json
import logging
import os
import sys

import asyncpg

from src.consolidation import config
from src.consolidation.backlog import render_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backlog_report")


async def _fetch_rows(pool, batch_run_id):
    rows = await pool.fetch(
        """
        SELECT ba.id, ba.batch_run_id, ba.lesson_a_id, ba.lesson_b_id,
               ba.cosine_similarity, ba.judge_model, ba.verdict, ba.direction,
               ba.confidence, ba.reasoning, ba.judged_at,
               la.title AS a_title, lb.title AS b_title
        FROM backlog_analysis ba
        JOIN lessons la ON la.id = ba.lesson_a_id
        JOIN lessons lb ON lb.id = ba.lesson_b_id
        WHERE ba.batch_run_id = $1
        ORDER BY ba.confidence DESC
        """,
        batch_run_id,
    )
    return [dict(r) for r in rows]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", required=True)
    p.add_argument("--format", choices=("markdown", "json", "both"), default="markdown")
    p.add_argument("--output", default=None)
    args = p.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL env var is required")
        return 2

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    try:
        rows = await _fetch_rows(pool, args.batch_id)
        if not rows:
            logger.warning("no rows found for batch_run_id=%r", args.batch_id)
            return 1

        md, data = render_report(rows, config)

        if args.format == "markdown":
            output = md
        elif args.format == "json":
            output = json.dumps(data, indent=2, default=str)
        else:  # both
            output = md + "\n\n---\n\n```json\n" + json.dumps(data, indent=2, default=str) + "\n```"

        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            logger.info("wrote %d chars to %s", len(output), args.output)
        else:
            print(output)

        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
