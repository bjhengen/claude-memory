"""Tests for eligibility classification of backlog_analysis rows."""

import pytest

from src.tools.backlog_apply import classify_eligibility


def _row(a_id=1, b_id=2, a_retired=False, b_retired=False,
         a_in_merges=False, b_in_merges=False, verdict="duplicate",
         direction=None, confidence=0.95, cosine=0.90,
         a_title="A", b_title="B", reasoning="r", judge_model="claude-haiku-4-5-20251001"):
    return {
        "lesson_a_id": a_id, "lesson_b_id": b_id,
        "a_retired": a_retired, "b_retired": b_retired,
        "a_in_merges": a_in_merges, "b_in_merges": b_in_merges,
        "verdict": verdict, "direction": direction,
        "confidence": confidence, "cosine_similarity": cosine,
        "a_title": a_title, "b_title": b_title,
        "reasoning": reasoning, "judge_model": judge_model,
    }


def test_classify_all_live_and_unmerged_is_eligible():
    rows = [_row(1, 2)]
    eligible, skip = classify_eligibility(rows)
    assert len(eligible) == 1
    assert len(skip) == 0


def test_classify_retired_side_skipped_with_reason():
    rows = [_row(1, 2, a_retired=True), _row(3, 4, b_retired=True)]
    eligible, skip = classify_eligibility(rows)
    assert len(eligible) == 0
    assert len(skip) == 2
    assert all(r["reason"] == "already_retired" for r in skip)


def test_classify_merged_side_skipped_with_reason():
    rows = [_row(1, 2, a_in_merges=True), _row(3, 4, b_in_merges=True)]
    eligible, skip = classify_eligibility(rows)
    assert len(eligible) == 0
    assert len(skip) == 2
    assert all(r["reason"] == "already_merged" for r in skip)


def test_classify_retired_beats_merged_when_both_apply():
    # A row where a is retired AND in merges — report retired (more specific)
    rows = [_row(1, 2, a_retired=True, a_in_merges=True)]
    eligible, skip = classify_eligibility(rows)
    assert len(skip) == 1
    assert skip[0]["reason"] == "already_retired"


def test_classify_mixed_batch():
    rows = [
        _row(1, 2),                           # eligible
        _row(3, 4, a_retired=True),           # skip retired
        _row(5, 6, b_in_merges=True),         # skip merged
        _row(7, 8),                           # eligible
    ]
    eligible, skip = classify_eligibility(rows)
    assert len(eligible) == 2
    assert len(skip) == 2
    assert {r["lesson_a_id"] for r in eligible} == {1, 7}
