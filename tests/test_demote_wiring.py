"""The demotion must change what the PIPELINE does, not just what a helper returns.

A unit-tested pure function that nothing calls is the classic gate that cannot
fail (lessons #1616/#1669). These tests drive the orchestrator with a mocked
judge and assert on the routed action.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.consolidation import config
from src.consolidation.actor import RoutingAction, decide_action
from src.consolidation.judge import JudgeVerdict


def _judge_reply(relationship, direction, confidence, reasoning):
    """Build an Anthropic response carrying one judge verdict."""
    block = MagicMock()
    block.text = json.dumps({
        "relationship": relationship, "direction": direction,
        "confidence": confidence, "reasoning": reasoning,
    })
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_additive_supersede_is_not_enqueued(monkeypatch, mock_anthropic):
    """The end-to-end point: an additive 'supersedes' must stop reaching the queue.

    Before this change such a pair was enqueued at ~0.92 confidence and sat
    there until a human rejected it — 81% of the queue's lifetime volume.
    """
    from src.consolidation import orchestrator

    mock_anthropic.messages.create.return_value = _judge_reply(
        "supersedes", "new→existing", 0.92,
        "New lesson identifies three failure modes that the existing lesson does not address.",
    )

    monkeypatch.setattr(
        orchestrator, "find_candidates",
        AsyncMock(return_value=[{
            "id": 999, "title": "existing", "content": "body", "cosine": 0.9,
        }]),
    )

    summary = await orchestrator.consolidate_at_log(
        pool=MagicMock(), anthropic=mock_anthropic,
        new_lesson_id=1000, new_title="new", new_content="body",
        new_embedding=[0.1] * 1536, project_id=None, new_source_agent="claude",
    )

    assert summary["action_taken"] == "ignored", (
        "an additive supersede must not be queued or auto-applied"
    )
    assert summary["best_verdict"] == "unrelated", (
        "demoted to complement, which _best_non_unrelated skips as non-actionable"
    )
    # The rule is ~87% precise, so a demotion must be REPORTED, not silent.
    assert summary["reason"] == "additive_supersede_demoted"
    assert summary["demoted_candidates"] == [999]


@pytest.mark.asyncio
async def test_genuine_supersede_still_routes_for_action(monkeypatch, mock_anthropic):
    """Guard against over-correction: real obsolescence must still act."""
    from src.consolidation import orchestrator

    mock_anthropic.messages.create.return_value = _judge_reply(
        "supersedes", "new→existing", 0.92,
        "The existing lesson's pinned IP has drifted and its command now causes an outage.",
    )
    monkeypatch.setattr(
        orchestrator, "find_candidates",
        AsyncMock(return_value=[{
            "id": 999, "title": "existing", "content": "body", "cosine": 0.9,
        }]),
    )
    monkeypatch.setattr(
        orchestrator, "execute_auto_supersede", AsyncMock(return_value=4242),
    )
    monkeypatch.setattr(
        orchestrator, "execute_enqueue", AsyncMock(return_value=77),
    )

    summary = await orchestrator.consolidate_at_log(
        pool=MagicMock(), anthropic=mock_anthropic,
        new_lesson_id=1000, new_title="new", new_content="body",
        new_embedding=[0.1] * 1536, project_id=None, new_source_agent="claude",
    )

    assert summary["best_verdict"] == "supersedes"
    assert summary["action_taken"] in ("auto_superseded", "queued"), (
        f"genuine supersede was swallowed: {summary}"
    )


def test_complement_routes_to_ignore():
    """The routing fact the demotion relies on."""
    v = JudgeVerdict("complement", None, 0.99, "adds detail")
    assert decide_action(v, config) == RoutingAction.IGNORE
