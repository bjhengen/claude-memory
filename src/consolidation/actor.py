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
