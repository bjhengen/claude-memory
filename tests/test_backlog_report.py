"""Tests for the backlog report renderer (pure function; no DB fixture needed)."""

import pytest

from src.consolidation.backlog import render_report


class _FakeThresholds:
    AUTO_MERGE_CONFIDENCE = 0.90
    AUTO_SUPERSEDE_CONFIDENCE = 0.95
    QUEUE_MIN_CONFIDENCE = 0.60


def _row(a_id, b_id, verdict, confidence, cosine=0.9, direction=None,
         reasoning="because", a_title=None, b_title=None):
    return {
        "lesson_a_id": a_id, "lesson_b_id": b_id,
        "cosine_similarity": cosine, "verdict": verdict,
        "direction": direction, "confidence": confidence,
        "reasoning": reasoning,
        "a_title": a_title or f"A{a_id}", "b_title": b_title or f"B{b_id}",
    }


def test_render_report_counts_all_verdicts():
    rows = [
        _row(1, 2, "duplicate", 0.95),
        _row(3, 4, "duplicate", 0.75),
        _row(5, 6, "supersedes", 0.97, direction="new→existing"),
        _row(7, 8, "contradicts", 0.72),
        _row(9, 10, "unrelated", 0.40),
    ]
    md, data = render_report(rows, _FakeThresholds)

    assert data["total_pairs"] == 5
    assert data["verdict_counts"]["duplicate"] == 2
    assert data["verdict_counts"]["supersedes"] == 1
    assert data["verdict_counts"]["contradicts"] == 1
    assert data["verdict_counts"]["unrelated"] == 1


def test_render_report_threshold_crossings_match_v5_logic():
    rows = [
        # Duplicate at 0.95 → auto-merges (≥0.90)
        _row(1, 2, "duplicate", 0.95),
        # Duplicate at 0.75 → enqueues (<0.90, ≥0.60)
        _row(3, 4, "duplicate", 0.75),
        # Duplicate at 0.50 → ignored (<0.60)
        _row(5, 6, "duplicate", 0.50),
        # Supersedes at 0.97 → auto-supersedes (≥0.95)
        _row(7, 8, "supersedes", 0.97, direction="new→existing"),
        # Supersedes at 0.91 → enqueues (<0.95, ≥0.60)
        _row(9, 10, "supersedes", 0.91, direction="new→existing"),
        # Contradicts at 0.70 → flags (≥0.60)
        _row(11, 12, "contradicts", 0.70),
        # Contradicts at 0.40 → ignored (<0.60)
        _row(13, 14, "contradicts", 0.40),
        # Unrelated always ignored
        _row(15, 16, "unrelated", 0.99),
    ]
    _, data = render_report(rows, _FakeThresholds)

    tc = data["threshold_crossings"]
    assert tc["auto_merge"] == 1
    assert tc["auto_supersede"] == 1
    assert tc["enqueue"] == 2  # duplicate@0.75 + supersede@0.91
    assert tc["flag_conflict"] == 1
    assert tc["ignore"] == 3  # duplicate@0.50 + contradicts@0.40 + unrelated@0.99


def test_render_report_markdown_contains_key_sections():
    rows = [
        _row(1, 2, "duplicate", 0.95),
        _row(3, 4, "unrelated", 0.10),
    ]
    md, _ = render_report(rows, _FakeThresholds)
    assert "# Backlog Analysis Report" in md
    assert "Verdict distribution" in md
    assert "Threshold crossings" in md
    assert "Top-20" in md or "Top 20" in md
