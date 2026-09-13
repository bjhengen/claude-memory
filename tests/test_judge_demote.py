"""The judge over-fires 'supersedes' on merely-additive pairs.

Lifetime queue outcomes through 2026-09-12: 13 approved of 68 proposals — an
81% rejection rate that the v7 'complement' verdict did not move, because v7
changed what is auto-applied, not what is proposed.

The signal is in the judge's OWN reasoning text. Across the 16 rejections in
the 2026-09-12 review, the judge repeatedly announced the additive
relationship while still labelling it 'supersedes': "does not address",
"adds a critical second axis", "expands into a complete framework".

Proposed in lesson #1327 (June), re-evidenced in #2049.
"""

import pytest

from src.consolidation.judge import JudgeVerdict, demote_additive_supersede


def _v(reasoning, relationship="supersedes", direction="new→existing", conf=0.92):
    return JudgeVerdict(relationship, direction, conf, reasoning)


@pytest.mark.parametrize("reasoning", [
    "New lesson identifies three failure modes that the existing lesson's four anti-patterns do not address.",
    "New lesson adds a critical second axis that fundamentally changes prioritization.",
    "The new lesson expands the existing lesson's single rule into a complete framework.",
    "New lesson identifies three additional failure modes and a validation method.",
    "New lesson generalizes the existing ad-hoc pattern to six more repos.",
    "This is a companion to the existing lesson, extending it with new detail.",
    "New lesson also covers the case the existing one omitted.",
])
def test_additive_tells_demote_supersede_to_complement(reasoning):
    """A supersede whose own reasoning describes addition is a complement."""
    out = demote_additive_supersede(_v(reasoning))
    assert out.relationship == "complement"
    assert out.direction is None, "complement carries no direction"
    assert "auto-demoted" in out.reasoning
    assert reasoning in out.reasoning, "original reasoning must be preserved"


@pytest.mark.parametrize("reasoning", [
    "New lesson reveals the pinned IP in the existing lesson has already drifted and is now wrong.",
    "Existing lesson incorrectly claims only v1 is available; the new lesson shows v2 is installed.",
    "The existing lesson's recommended command causes an outage and is replaced by a safe one.",
    "New lesson corrects a false premise that invalidated the existing lesson's reasoning.",
])
def test_genuine_supersedes_are_untouched(reasoning):
    """Obsolescence language must survive: these are real supersessions."""
    out = demote_additive_supersede(_v(reasoning))
    assert out.relationship == "supersedes"
    assert out.direction == "new→existing"
    assert out.reasoning == reasoning


def test_only_supersedes_is_demoted():
    """Other relationships pass through untouched even with additive words."""
    for rel, direction in [("duplicate", None), ("complement", None),
                           ("contradicts", None), ("unrelated", None)]:
        v = _v("this one adds detail and also extends the other", rel, direction)
        out = demote_additive_supersede(v)
        assert out.relationship == rel, f"{rel} must not be rewritten"


def test_matching_is_case_insensitive_and_word_bounded():
    """'adds' triggers; a word merely containing it must not."""
    assert demote_additive_supersede(
        _v("ADDS a second axis")).relationship == "complement"
    # 'addstress'/'paddsee' are not words, but 'ladder'/'address' are the real
    # risk: 'address' contains 'adds' only as a substring with a boundary break.
    assert demote_additive_supersede(
        _v("The new lesson makes the old address format obsolete and wrong")
    ).relationship == "supersedes"


def test_demotion_is_idempotent():
    """Demoting an already-demoted verdict changes nothing further."""
    once = demote_additive_supersede(_v("it adds a new axis"))
    twice = demote_additive_supersede(once)
    assert twice.relationship == "complement"
    assert twice.reasoning == once.reasoning


def test_confidence_is_preserved():
    """Demotion reclassifies; it does not editorialise the confidence."""
    out = demote_additive_supersede(_v("adds more detail", conf=0.87))
    assert out.confidence == 0.87


def test_more_complete_is_deliberately_not_a_tell():
    """Backtested at 1 rejected / 1 approved over all 68 decided entries.

    A coin-flip phrase adds noise without signal. This test exists so the
    omission reads as a measured decision rather than an oversight — if
    someone re-adds it, they should re-measure first.
    """
    out = demote_additive_supersede(
        _v("New lesson provides a safer, more complete workaround.")
    )
    assert out.relationship == "supersedes"
