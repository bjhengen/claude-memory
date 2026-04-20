"""Orchestrator: the single entrypoint called from log_lesson after insert."""

import asyncio
import logging

import asyncpg
from anthropic import AsyncAnthropic

from src.consolidation import config
from src.consolidation.candidates import find_candidates
from src.consolidation.judge import adjudicate, JudgeVerdict
from src.consolidation.actor import (
    RoutingAction, decide_action,
    execute_auto_merge, execute_auto_supersede,
    execute_enqueue, execute_flag_conflict,
)

logger = logging.getLogger(__name__)


async def _judge_pair(anthropic, new_title, new_content, cand):
    """Call the judge for one candidate and return (candidate, verdict)."""
    verdict = await adjudicate(
        anthropic,
        new_title=new_title, new_content=new_content,
        candidate_title=cand["title"], candidate_content=cand["content"],
        model=config.JUDGE_MODEL, timeout=config.JUDGE_TIMEOUT_SECONDS,
    )
    return cand, verdict


def _best_non_unrelated(pairs):
    """Highest-confidence non-unrelated verdict; ties broken by higher cosine."""
    scored = [(c, v) for c, v in pairs if v.relationship != "unrelated"]
    if not scored:
        return None
    scored.sort(key=lambda cv: (cv[1].confidence, cv[0].get("cosine", 0.0)), reverse=True)
    return scored[0]


async def consolidate_at_log(
    pool: asyncpg.Pool,
    anthropic: AsyncAnthropic,
    new_lesson_id: int,
    new_title: str,
    new_content: str,
    new_embedding: list[float],
    project_id: int | None,
) -> dict:
    """
    Run consolidation for a just-inserted lesson. Returns a summary dict:
    {
      "action_taken": "ignored" | "auto_merged" | "auto_superseded" | "queued" | "flagged",
      "candidate_count": int,
      "best_verdict": str | None,
      "confidence": float | None,
      "merge_id": int | None,       # for auto_merged/auto_superseded
      "queue_id": int | None,       # for queued
      "conflict_id": int | None,    # for flagged
    }

    Failure modes always return {"action_taken": "ignored", ...} + log a warning.
    The caller (log_lesson) treats any return as non-fatal.
    """
    if not config.ENABLED:
        return {"action_taken": "ignored", "reason": "disabled",
                "candidate_count": 0, "best_verdict": None, "confidence": None}

    try:
        cands = await find_candidates(
            pool, query_embedding=new_embedding, new_lesson_id=new_lesson_id,
            project_id=project_id, cosine_threshold=config.CANDIDATE_COSINE,
            top_k=config.CANDIDATE_TOP_K,
        )
    except Exception as e:
        logger.warning("candidate_finder failed: %s", e)
        return {"action_taken": "ignored", "reason": "finder_error",
                "candidate_count": 0, "best_verdict": None, "confidence": None}

    if not cands:
        return {"action_taken": "ignored", "reason": "no_candidates",
                "candidate_count": 0, "best_verdict": None, "confidence": None}

    # Judge all candidates in parallel
    try:
        pairs = await asyncio.gather(
            *[_judge_pair(anthropic, new_title, new_content, c) for c in cands]
        )
    except Exception as e:
        logger.warning("judge fanout failed: %s", e)
        return {"action_taken": "ignored", "reason": "judge_error",
                "candidate_count": len(cands), "best_verdict": None, "confidence": None}

    best = _best_non_unrelated(pairs)
    if best is None:
        return {"action_taken": "ignored", "reason": "all_unrelated",
                "candidate_count": len(cands), "best_verdict": "unrelated",
                "confidence": None}

    cand, verdict = best
    action = decide_action(verdict, config)
    summary = {
        "action_taken": None, "candidate_count": len(cands),
        "best_verdict": verdict.relationship, "confidence": verdict.confidence,
        "merge_id": None, "queue_id": None, "conflict_id": None,
    }

    try:
        if action == RoutingAction.IGNORE:
            summary["action_taken"] = "ignored"
        elif action == RoutingAction.AUTO_MERGE:
            merge_id = await execute_auto_merge(
                pool, new_lesson_id=new_lesson_id, canonical_id=cand["id"],
                verdict=verdict, cosine=float(cand["cosine"]),
                judge_model=config.JUDGE_MODEL,
            )
            summary.update(action_taken="auto_merged", merge_id=merge_id)
        elif action == RoutingAction.AUTO_SUPERSEDE:
            if verdict.direction == "new→existing":
                merge_id = await execute_auto_supersede(
                    pool, new_lesson_id=new_lesson_id, existing_lesson_id=cand["id"],
                    verdict=verdict, cosine=float(cand["cosine"]),
                    judge_model=config.JUDGE_MODEL,
                )
                summary.update(action_taken="auto_superseded", merge_id=merge_id)
            else:  # existing→new: existing covers new's claim — retire new as 'merged'
                merge_id = await execute_auto_merge(
                    pool, new_lesson_id=new_lesson_id, canonical_id=cand["id"],
                    verdict=verdict, cosine=float(cand["cosine"]),
                    judge_model=config.JUDGE_MODEL,
                )
                summary.update(action_taken="auto_merged", merge_id=merge_id)
        elif action == RoutingAction.ENQUEUE:
            proposed = "superseded" if verdict.relationship == "supersedes" else "merged"
            queue_id = await execute_enqueue(
                pool, new_lesson_id=new_lesson_id, candidate_lesson_id=cand["id"],
                verdict=verdict, cosine=float(cand["cosine"]),
                judge_model=config.JUDGE_MODEL, proposed_action=proposed,
            )
            summary.update(action_taken="queued", queue_id=queue_id)
        elif action == RoutingAction.FLAG_CONFLICT:
            conflict_id = await execute_flag_conflict(
                pool, new_lesson_id=new_lesson_id, candidate_lesson_id=cand["id"],
                verdict=verdict, cosine=float(cand["cosine"]),
                judge_model=config.JUDGE_MODEL,
            )
            summary.update(action_taken="flagged", conflict_id=conflict_id)
    except Exception as e:
        logger.warning("actor execution failed for action=%s: %s", action, e)
        summary["action_taken"] = "ignored"
        summary["reason"] = f"actor_error:{type(e).__name__}"

    logger.info(
        "consolidation: candidates=%d verdict=%s confidence=%s action=%s",
        summary["candidate_count"], summary["best_verdict"],
        summary["confidence"], summary["action_taken"],
    )
    return summary
