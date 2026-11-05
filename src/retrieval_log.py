"""Record which stored memory a read returned, per client.

Ids only: query text and returned content are never stored. Logging is best
effort, so a failed or slow write costs the record and never the read.
"""

import asyncio
import json
import logging
from typing import Iterable

from src.identity import stamp

logger = logging.getLogger(__name__)

LOG_TIMEOUT_S = 0.25


async def log_retrieval(ctx, tool: str, items: Iterable[tuple[str, int]] = ()) -> None:
    """Write one retrieval_log row. items are (type, id) pairs; empty is valid."""
    try:
        pairs = [{"type": kind, "id": item_id} for kind, item_id in items]
        source_agent, source_client_id = stamp()
        await asyncio.wait_for(
            ctx.request_context.lifespan_context.db.execute(
                """
                INSERT INTO retrieval_log
                    (tool, source_agent, source_client_id, result_count, items)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                tool, source_agent, source_client_id, len(pairs), json.dumps(pairs),
            ),
            timeout=LOG_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("retrieval log write failed for %s: %s", tool, type(exc).__name__)
