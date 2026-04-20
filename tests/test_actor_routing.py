"""Tests for the pure routing-decision function in the actor."""

import pytest

from src.consolidation.judge import JudgeVerdict
from src.consolidation.actor import decide_action, RoutingAction


class DummyConfig:
    AUTO_MERGE_CONFIDENCE = 0.90
    AUTO_SUPERSEDE_CONFIDENCE = 0.95
    QUEUE_MIN_CONFIDENCE = 0.60


def test_duplicate_at_auto_threshold_auto_merges():
    v = JudgeVerdict("duplicate", None, 0.92, "same thing")
    assert decide_action(v, DummyConfig) == RoutingAction.AUTO_MERGE


def test_duplicate_below_auto_but_above_queue_enqueues():
    v = JudgeVerdict("duplicate", None, 0.75, "similar")
    assert decide_action(v, DummyConfig) == RoutingAction.ENQUEUE


def test_duplicate_below_queue_threshold_ignored():
    v = JudgeVerdict("duplicate", None, 0.50, "weak")
    assert decide_action(v, DummyConfig) == RoutingAction.IGNORE


def test_supersedes_requires_higher_confidence_than_merge():
    v_high = JudgeVerdict("supersedes", "new→existing", 0.96, "replaces")
    assert decide_action(v_high, DummyConfig) == RoutingAction.AUTO_SUPERSEDE

    v_between = JudgeVerdict("supersedes", "new→existing", 0.91, "replaces")
    # 0.91 is above AUTO_MERGE (0.90) but below AUTO_SUPERSEDE (0.95) — enqueues
    assert decide_action(v_between, DummyConfig) == RoutingAction.ENQUEUE


def test_contradicts_always_flags_when_above_queue_min():
    v = JudgeVerdict("contradicts", None, 0.60, "opposite")
    assert decide_action(v, DummyConfig) == RoutingAction.FLAG_CONFLICT


def test_contradicts_below_queue_min_ignored():
    v = JudgeVerdict("contradicts", None, 0.30, "weak")
    assert decide_action(v, DummyConfig) == RoutingAction.IGNORE


def test_unrelated_always_ignored():
    v = JudgeVerdict("unrelated", None, 0.95, "nope")
    assert decide_action(v, DummyConfig) == RoutingAction.IGNORE
