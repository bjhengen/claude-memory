"""The judge taxonomy must include a 'complement' verdict.

Review 2026-07-06 / lesson #1327: with only duplicate|supersedes|contradicts|
unrelated, topically-overlapping-but-additive pairs had no correct bucket —
candidates are pre-filtered to cosine >= 0.85, so "unrelated" rarely fires and
additive pairs were funneled into "supersedes" (~79% of supersede verdicts at
the June queue review were false). Complement is non-actionable: it must parse,
route to IGNORE, and never shadow an actionable verdict from another candidate.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.consolidation.actor import decide_action, RoutingAction
from src.consolidation.judge import adjudicate, JudgeVerdict, _SYSTEM_PROMPT
from src.consolidation.orchestrator import _best_non_unrelated


class _FakeConfig:
    AUTO_MERGE_CONFIDENCE = 0.90
    AUTO_SUPERSEDE_CONFIDENCE = 0.95
    QUEUE_MIN_CONFIDENCE = 0.60


def _mock_response(payload: dict):
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    return resp


@pytest.mark.asyncio
async def test_judge_parses_complement_verdict(mock_anthropic):
    mock_anthropic.messages.create.return_value = _mock_response({
        "relationship": "complement",
        "direction": None,
        "confidence": 0.87,
        "reasoning": "Both true; new adds GPU-specific detail on top."
    })

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="B", candidate_content="y",
        model="claude-haiku-4-5-20251001", timeout=2.0,
    )

    assert verdict.relationship == "complement"
    assert verdict.confidence == 0.87
    assert verdict.direction is None


def test_prompt_defines_complement_and_discriminator():
    """The prompt must offer the complement bucket and steer additive pairs
    away from supersedes."""
    assert '"complement"' in _SYSTEM_PROMPT
    assert "complement" in _SYSTEM_PROMPT.split('"supersedes"', 1)[1].lower()


def test_decide_action_routes_complement_to_ignore():
    verdict = JudgeVerdict("complement", None, 0.99, "additive")
    assert decide_action(verdict, _FakeConfig) == RoutingAction.IGNORE


def test_best_verdict_selection_skips_complement():
    """A high-confidence complement must not shadow an actionable duplicate
    from another candidate."""
    complement_pair = ({"id": 1, "cosine": 0.95},
                       JudgeVerdict("complement", None, 0.97, "additive"))
    duplicate_pair = ({"id": 2, "cosine": 0.88},
                      JudgeVerdict("duplicate", None, 0.91, "same advice"))

    best = _best_non_unrelated([complement_pair, duplicate_pair])

    assert best is not None
    assert best[1].relationship == "duplicate"


def test_best_verdict_selection_returns_none_when_only_complement():
    pairs = [({"id": 1, "cosine": 0.9},
              JudgeVerdict("complement", None, 0.9, "additive"))]
    assert _best_non_unrelated(pairs) is None
