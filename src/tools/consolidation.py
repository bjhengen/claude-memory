"""v5 consolidation MCP tools: queue management, conflict resolution, and undo."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.consolidation.actor import (
    execute_auto_merge, execute_auto_supersede,
)
from src.consolidation.judge import JudgeVerdict


async def _clear_pending_annotation_text(conn, entity_type, entity_id, queue_id):
    """Strip the '⏸ Consolidation pending (queue #{Q})' block from an entity's
    annotation, preserving other content. Delete the annotation row if empty after."""
    marker = f"⏸ Consolidation pending (queue #{queue_id}):"
    row = await conn.fetchrow(
        "SELECT id, note FROM annotations WHERE entity_type=$1 AND entity_id=$2",
        entity_type, entity_id,
    )
    if not row:
        return
    note = row["note"]
    if marker not in note:
        return
    lines = [ln for ln in note.split("\n") if marker not in ln]
    cleaned = "\n".join(lines).strip()
    while "\n\n---\n\n---" in cleaned:
        cleaned = cleaned.replace("\n\n---\n\n---", "\n\n---")
    cleaned = cleaned.strip("\n").strip()
    while cleaned.endswith("---"):
        cleaned = cleaned[:-3].rstrip()
    while cleaned.startswith("---"):
        cleaned = cleaned[3:].lstrip()
    if not cleaned:
        await conn.execute("DELETE FROM annotations WHERE id=$1", row["id"])
    else:
        await conn.execute(
            "UPDATE annotations SET note=$1, updated_at=NOW() WHERE id=$2",
            cleaned, row["id"],
        )


@mcp.tool()
async def list_pending_consolidations(
    project: str = None,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """
    List consolidation proposals awaiting human review.

    Args:
        project: Filter by project name (optional)
        limit: Maximum entries to return (default 20)
    """
    app = ctx.request_context.lifespan_context

    project_filter = ""
    params = [limit]
    if project:
        from src.helpers import resolve_project_id
        pid = await resolve_project_id(app.db, project)
        if pid is None:
            return json.dumps({"results": [], "message": f"project '{project}' not found"})
        project_filter = "AND (ln.project_id = $2 OR lc.project_id = $2)"
        params.append(pid)

    rows = await app.db.fetch(
        f"""
        SELECT q.id AS queue_id, q.new_lesson_id, q.candidate_lesson_id,
               q.proposed_action, q.proposed_direction, q.judge_confidence,
               q.judge_reasoning, q.cosine_similarity, q.enqueued_at,
               ln.title AS new_title, lc.title AS candidate_title,
               EXTRACT(EPOCH FROM (NOW() - q.enqueued_at)) / 86400.0 AS age_days
        FROM consolidation_queue q
        JOIN lessons ln ON ln.id = q.new_lesson_id
        JOIN lessons lc ON lc.id = q.candidate_lesson_id
        WHERE q.decided_at IS NULL
          {project_filter}
        ORDER BY q.enqueued_at ASC
        LIMIT $1
        """,
        *params,
    )

    results = [
        {
            "queue_id": r["queue_id"],
            "new_lesson": {"id": r["new_lesson_id"], "title": r["new_title"]},
            "candidate_lesson": {"id": r["candidate_lesson_id"], "title": r["candidate_title"]},
            "proposed_action": r["proposed_action"],
            "proposed_direction": r["proposed_direction"],
            "confidence": float(r["judge_confidence"]),
            "reasoning": r["judge_reasoning"],
            "cosine": float(r["cosine_similarity"]),
            "enqueued_at": r["enqueued_at"].isoformat(),
            "age_days": round(float(r["age_days"]), 2),
        }
        for r in rows
    ]
    return json.dumps({"pending": results, "count": len(results)})


@mcp.tool()
async def approve_consolidation(
    queue_id: int,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Approve a pending consolidation proposal. Executes the merge or supersede
    exactly as proposed and clears the pending annotations.

    Args:
        queue_id: ID of the consolidation_queue entry to approve
        reviewer: Who is approving (for audit trail)
    """
    app = ctx.request_context.lifespan_context

    q = await app.db.fetchrow(
        "SELECT * FROM consolidation_queue WHERE id=$1",
        queue_id,
    )
    if q is None:
        return json.dumps({"error": f"queue entry {queue_id} not found"})
    if q["decided_at"] is not None:
        return json.dumps({"error": f"queue entry {queue_id} already decided: {q['decision']}"})

    # Refuse if canonical side was retired after enqueue
    canonical_candidate_side = (
        q["candidate_lesson_id"] if q["proposed_action"] == "merged" or
        q["proposed_direction"] == "existing→new"
        else q["new_lesson_id"]
    )
    retired = await app.db.fetchrow(
        "SELECT retired_at FROM lessons WHERE id=$1", canonical_candidate_side,
    )
    if retired and retired["retired_at"] is not None:
        return json.dumps({
            "error": f"canonical lesson {canonical_candidate_side} has been retired; "
                     f"call reject_consolidation({queue_id}) instead",
        })

    verdict = JudgeVerdict(
        relationship="duplicate" if q["proposed_action"] == "merged" else "supersedes",
        direction=q["proposed_direction"],
        confidence=float(q["judge_confidence"]),
        reasoning=q["judge_reasoning"],
    )

    reviewer = reviewer or "unknown"

    if q["proposed_action"] == "merged":
        merge_id = await execute_auto_merge(
            app.db, new_lesson_id=q["new_lesson_id"], canonical_id=q["candidate_lesson_id"],
            verdict=verdict, cosine=float(q["cosine_similarity"]),
            judge_model=q["judge_model"], decided_by=reviewer, auto_decided=False,
        )
    else:  # 'superseded'
        if q["proposed_direction"] == "new→existing":
            merge_id = await execute_auto_supersede(
                app.db, new_lesson_id=q["new_lesson_id"], existing_lesson_id=q["candidate_lesson_id"],
                verdict=verdict, cosine=float(q["cosine_similarity"]),
                judge_model=q["judge_model"], decided_by=reviewer, auto_decided=False,
            )
        else:  # 'existing→new': retire new as merged into existing
            merge_id = await execute_auto_merge(
                app.db, new_lesson_id=q["new_lesson_id"], canonical_id=q["candidate_lesson_id"],
                verdict=verdict, cosine=float(q["cosine_similarity"]),
                judge_model=q["judge_model"], decided_by=reviewer, auto_decided=False,
            )

    # Mark queue decided + clear both pending annotations
    async with app.db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE consolidation_queue SET decided_at=NOW(), decided_by=$1, decision='approved' "
                "WHERE id=$2",
                reviewer, queue_id,
            )
            await _clear_pending_annotation_text(conn, "lesson", q["new_lesson_id"], queue_id)
            await _clear_pending_annotation_text(conn, "lesson", q["candidate_lesson_id"], queue_id)

    return json.dumps({
        "success": True,
        "queue_id": queue_id,
        "merge_id": merge_id,
        "action": q["proposed_action"],
        "reviewer": reviewer,
    })


@mcp.tool()
async def reject_consolidation(
    queue_id: int,
    reason: str = None,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Reject a pending consolidation proposal. Leaves both lessons unchanged;
    clears the pending annotations on both.

    Args:
        queue_id: ID of the consolidation_queue entry to reject
        reason: Optional explanation
        reviewer: Who is rejecting
    """
    app = ctx.request_context.lifespan_context

    q = await app.db.fetchrow("SELECT * FROM consolidation_queue WHERE id=$1", queue_id)
    if q is None:
        return json.dumps({"error": f"queue entry {queue_id} not found"})
    if q["decided_at"] is not None:
        return json.dumps({"error": f"queue entry {queue_id} already decided"})

    reviewer = reviewer or "unknown"

    async with app.db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE consolidation_queue SET decided_at=NOW(), decided_by=$1, "
                "decision='rejected', decision_note=$2 WHERE id=$3",
                reviewer, reason, queue_id,
            )
            await _clear_pending_annotation_text(conn, "lesson", q["new_lesson_id"], queue_id)
            await _clear_pending_annotation_text(conn, "lesson", q["candidate_lesson_id"], queue_id)

    return json.dumps({
        "success": True, "queue_id": queue_id, "reviewer": reviewer,
        "reason": reason,
    })
