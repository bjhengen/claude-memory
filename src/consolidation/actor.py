"""Actor: routes judge verdicts to DB mutations.

Split into:
- decide_action: pure function mapping (verdict, config) -> RoutingAction
- execute_action: async DB-mutating function that performs the routed action
"""

from enum import Enum

from src.consolidation.judge import JudgeVerdict


class RoutingAction(str, Enum):
    AUTO_MERGE = "auto_merge"
    AUTO_SUPERSEDE = "auto_supersede"
    ENQUEUE = "enqueue"
    FLAG_CONFLICT = "flag_conflict"
    IGNORE = "ignore"


def decide_action(verdict: JudgeVerdict, config) -> RoutingAction:
    """Route a judge verdict to the action the actor should take."""
    rel = verdict.relationship
    conf = verdict.confidence

    if rel == "unrelated":
        return RoutingAction.IGNORE

    if rel == "duplicate":
        if conf >= config.AUTO_MERGE_CONFIDENCE:
            return RoutingAction.AUTO_MERGE
        if conf >= config.QUEUE_MIN_CONFIDENCE:
            return RoutingAction.ENQUEUE
        return RoutingAction.IGNORE

    if rel == "supersedes":
        if conf >= config.AUTO_SUPERSEDE_CONFIDENCE:
            return RoutingAction.AUTO_SUPERSEDE
        if conf >= config.QUEUE_MIN_CONFIDENCE:
            return RoutingAction.ENQUEUE
        return RoutingAction.IGNORE

    if rel == "contradicts":
        if conf >= config.QUEUE_MIN_CONFIDENCE:
            return RoutingAction.FLAG_CONFLICT
        return RoutingAction.IGNORE

    return RoutingAction.IGNORE


import asyncpg


async def _annotate(conn, entity_type: str, entity_id: int, note: str) -> None:
    """Insert or append to an annotation. Uses the v4 annotations-append pattern."""
    from datetime import datetime, timezone
    existing = await conn.fetchrow(
        "SELECT id, note FROM annotations WHERE entity_type=$1 AND entity_id=$2",
        entity_type, entity_id,
    )
    if existing:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        combined = f"{existing['note']}\n\n---\n[{ts}] {note}"
        await conn.execute(
            "UPDATE annotations SET note=$1, updated_at=NOW() WHERE id=$2",
            combined, existing["id"],
        )
    else:
        await conn.execute(
            "INSERT INTO annotations (entity_type, entity_id, note) VALUES ($1,$2,$3)",
            entity_type, entity_id, note,
        )


async def execute_auto_merge(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    canonical_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
    decided_by: str | None = None,
    auto_decided: bool = True,
) -> int:
    """
    Retire the new lesson, transfer its counters + tags into canonical,
    repoint annotations, and write an audit row. Returns the new lesson_merges.id.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            new = await conn.fetchrow(
                "SELECT upvotes, downvotes, tags, severity FROM lessons WHERE id=$1",
                new_lesson_id,
            )
            if new is None:
                raise ValueError(f"new lesson {new_lesson_id} not found")
            new_up = new["upvotes"] or 0
            new_down = new["downvotes"] or 0

            # Retire the new lesson with a pointer to canonical
            reason = f"merged into {canonical_id}"
            await conn.execute(
                "UPDATE lessons SET retired_at=NOW(), retired_reason=$1 WHERE id=$2",
                reason, new_lesson_id,
            )

            # Transfer counters to canonical
            await conn.execute(
                """
                UPDATE lessons
                SET upvotes = COALESCE(upvotes,0) + $1,
                    downvotes = COALESCE(downvotes,0) + $2
                WHERE id = $3
                """,
                new_up, new_down, canonical_id,
            )

            # Union tags (no duplicates)
            await conn.execute(
                """
                UPDATE lessons
                SET tags = (SELECT ARRAY(SELECT DISTINCT unnest(tags || $1::text[])))
                WHERE id = $2
                """,
                new["tags"] or [], canonical_id,
            )

            # Severity escalation: canonical takes max(canonical.severity, new.severity)
            # Ordering: critical > important > tip
            await _escalate_severity(conn, canonical_id, new["severity"])

            # Repoint annotations from new -> canonical
            await conn.execute(
                "UPDATE annotations SET entity_id=$1 "
                "WHERE entity_type='lesson' AND entity_id=$2",
                canonical_id, new_lesson_id,
            )

            # Audit row
            audit = await conn.fetchrow(
                """
                INSERT INTO lesson_merges
                  (canonical_id, merged_id, action, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity, auto_decided, decided_by,
                   transferred_upvotes, transferred_downvotes)
                VALUES ($1,$2,'merged',$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id
                """,
                canonical_id, new_lesson_id, judge_model, verdict.confidence,
                verdict.reasoning, cosine, auto_decided, decided_by, new_up, new_down,
            )

            await _annotate(
                conn, "lesson", canonical_id,
                f"📎 Merged from lesson #{new_lesson_id}: {verdict.reasoning}",
            )

            return audit["id"]


_SEVERITY_ORDER = {"tip": 0, "important": 1, "critical": 2}


async def _escalate_severity(conn, canonical_id: int, incoming_severity: str) -> None:
    """Update canonical severity to the higher of (current, incoming)."""
    if not incoming_severity:
        return
    row = await conn.fetchrow("SELECT severity FROM lessons WHERE id=$1", canonical_id)
    if not row:
        return
    current = row["severity"] or "tip"
    if _SEVERITY_ORDER.get(incoming_severity, 0) > _SEVERITY_ORDER.get(current, 0):
        await conn.execute(
            "UPDATE lessons SET severity=$1 WHERE id=$2",
            incoming_severity, canonical_id,
        )


async def execute_auto_supersede(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    existing_lesson_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
    decided_by: str | None = None,
    auto_decided: bool = True,
) -> int:
    """
    Direction is new→existing: existing is retired, new is canonical.
    Transfer existing's counters + tags to new.
    For direction existing→new, caller should use execute_auto_merge with
    new_lesson_id as the merged_id (existing is canonical).
    """
    if verdict.direction != "new→existing":
        raise ValueError(
            "execute_auto_supersede only handles direction='new→existing'; "
            "use execute_auto_merge for 'existing→new'"
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT upvotes, downvotes, tags FROM lessons WHERE id=$1",
                existing_lesson_id,
            )
            if existing is None:
                raise ValueError(f"existing lesson {existing_lesson_id} not found")
            ex_up = existing["upvotes"] or 0
            ex_down = existing["downvotes"] or 0

            reason = f"superseded by {new_lesson_id}"
            await conn.execute(
                "UPDATE lessons SET retired_at=NOW(), retired_reason=$1 WHERE id=$2",
                reason, existing_lesson_id,
            )

            await conn.execute(
                "UPDATE lessons SET upvotes=COALESCE(upvotes,0)+$1, "
                "downvotes=COALESCE(downvotes,0)+$2 WHERE id=$3",
                ex_up, ex_down, new_lesson_id,
            )

            await conn.execute(
                "UPDATE lessons SET tags=(SELECT ARRAY(SELECT DISTINCT unnest(tags || $1::text[]))) "
                "WHERE id=$2",
                existing["tags"] or [], new_lesson_id,
            )

            # Severity escalation: new takes max(new.severity, existing.severity)
            ex_sev = await conn.fetchval(
                "SELECT severity FROM lessons WHERE id=$1", existing_lesson_id,
            )
            await _escalate_severity(conn, new_lesson_id, ex_sev)

            await conn.execute(
                "UPDATE annotations SET entity_id=$1 "
                "WHERE entity_type='lesson' AND entity_id=$2",
                new_lesson_id, existing_lesson_id,
            )

            audit = await conn.fetchrow(
                """
                INSERT INTO lesson_merges
                  (canonical_id, merged_id, action, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity, auto_decided, decided_by,
                   transferred_upvotes, transferred_downvotes)
                VALUES ($1,$2,'superseded',$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id
                """,
                new_lesson_id, existing_lesson_id, judge_model, verdict.confidence,
                verdict.reasoning, cosine, auto_decided, decided_by, ex_up, ex_down,
            )

            await _annotate(
                conn, "lesson", new_lesson_id,
                f"↗ Supersedes lesson #{existing_lesson_id}: {verdict.reasoning}",
            )

            return audit["id"]


async def execute_enqueue(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    candidate_lesson_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
    proposed_action: str,  # 'merged' | 'superseded'
) -> int:
    """Create a queue row and annotate both lessons with pending notices."""
    if proposed_action not in ("merged", "superseded"):
        raise ValueError(f"invalid proposed_action: {proposed_action}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO consolidation_queue
                  (new_lesson_id, candidate_lesson_id, proposed_action,
                   proposed_direction, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                RETURNING id
                """,
                new_lesson_id, candidate_lesson_id, proposed_action,
                verdict.direction, judge_model, verdict.confidence,
                verdict.reasoning, cosine,
            )
            queue_id = row["id"]

            pending_new = (
                f"⏸ Consolidation pending (queue #{queue_id}): proposed {proposed_action} "
                f"with lesson #{candidate_lesson_id}. Confidence {verdict.confidence:.2f}. "
                f"Run approve_consolidation({queue_id}) or reject_consolidation({queue_id})."
            )
            pending_cand = (
                f"⏸ Consolidation pending (queue #{queue_id}): proposed {proposed_action} "
                f"with lesson #{new_lesson_id}. Confidence {verdict.confidence:.2f}. "
                f"Run approve_consolidation({queue_id}) or reject_consolidation({queue_id})."
            )
            await _annotate(conn, "lesson", new_lesson_id, pending_new)
            await _annotate(conn, "lesson", candidate_lesson_id, pending_cand)

            return queue_id


async def execute_flag_conflict(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    candidate_lesson_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
) -> int:
    """
    Insert a conflict row with canonical ordering (a_id < b_id) and annotate both.
    Returns the new lesson_conflicts.id.
    """
    a_id, b_id = sorted([new_lesson_id, candidate_lesson_id])

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO lesson_conflicts
                  (lesson_a_id, lesson_b_id, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (lesson_a_id, lesson_b_id) DO UPDATE
                  SET judge_confidence = EXCLUDED.judge_confidence,
                      judge_reasoning = EXCLUDED.judge_reasoning,
                      cosine_similarity = EXCLUDED.cosine_similarity,
                      flagged_at = NOW()
                RETURNING id
                """,
                a_id, b_id, judge_model, verdict.confidence,
                verdict.reasoning, cosine,
            )
            conflict_id = row["id"]

            def note_for(other: int) -> str:
                return (
                    f"⚠ Conflicts with lesson #{other} (confidence {verdict.confidence:.2f}): "
                    f"{verdict.reasoning}. Resolve via resolve_conflict({conflict_id})."
                )
            await _annotate(conn, "lesson", a_id, note_for(b_id))
            await _annotate(conn, "lesson", b_id, note_for(a_id))

            return conflict_id
