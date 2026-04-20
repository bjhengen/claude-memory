"""Tests for the consolidation judge."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.consolidation.judge import adjudicate, JudgeVerdict


def _mock_response(payload: dict):
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    return resp


@pytest.mark.asyncio
async def test_judge_parses_duplicate_verdict(mock_anthropic):
    mock_anthropic.messages.create.return_value = _mock_response({
        "relationship": "duplicate",
        "direction": None,
        "confidence": 0.92,
        "reasoning": "Both say the same thing."
    })

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="A'", candidate_content="x'",
        model="claude-haiku-4-5-20251001", timeout=2.0,
    )

    assert verdict.relationship == "duplicate"
    assert verdict.confidence == 0.92
    assert verdict.direction is None


@pytest.mark.asyncio
async def test_judge_parses_supersedes_with_direction(mock_anthropic):
    mock_anthropic.messages.create.return_value = _mock_response({
        "relationship": "supersedes",
        "direction": "new→existing",
        "confidence": 0.96,
        "reasoning": "New replaces old."
    })

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="B", candidate_content="y",
        model="claude-haiku-4-5-20251001", timeout=2.0,
    )

    assert verdict.relationship == "supersedes"
    assert verdict.direction == "new→existing"


@pytest.mark.asyncio
async def test_judge_returns_unrelated_on_malformed_json(mock_anthropic):
    resp = MagicMock()
    resp.content = [MagicMock(text="this is not json")]
    mock_anthropic.messages.create.return_value = resp

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="B", candidate_content="y",
        model="claude-haiku-4-5-20251001", timeout=2.0,
    )

    assert verdict.relationship == "unrelated"
    assert verdict.confidence == 0.0


@pytest.mark.asyncio
async def test_judge_returns_unrelated_on_timeout(mock_anthropic):
    async def slow(*a, **kw):
        await asyncio.sleep(5)

    mock_anthropic.messages.create.side_effect = slow

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="B", candidate_content="y",
        model="claude-haiku-4-5-20251001", timeout=0.1,
    )

    assert verdict.relationship == "unrelated"
    assert verdict.confidence == 0.0
    assert "timeout" in verdict.reasoning.lower()
