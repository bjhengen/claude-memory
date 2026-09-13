"""LLM judge: classifies a candidate pair as duplicate / supersedes / complement / contradicts / unrelated."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


Relationship = Literal["duplicate", "supersedes", "complement", "contradicts", "unrelated"]
Direction = Literal["new→existing", "existing→new"] | None


@dataclass
class JudgeVerdict:
    relationship: Relationship
    direction: Direction
    confidence: float  # 0.0–1.0
    reasoning: str


_SYSTEM_PROMPT = """\
You classify the relationship between two short developer lessons.

Given two lessons ("new" and "existing"), return ONLY a JSON object:
{
  "relationship": "duplicate" | "supersedes" | "complement" | "contradicts" | "unrelated",
  "direction": "new→existing" | "existing→new" | null,
  "confidence": 0.0 to 1.0,
  "reasoning": "<one short sentence>"
}

Rules:
- "duplicate": both lessons convey substantively the same advice. direction=null.
- "supersedes": one lesson makes the other OBSOLETE — following the old one would now be wrong or wasteful (e.g., a newer workaround replaces a broken one, a config value was corrected). direction is required: "new→existing" means new supersedes existing; "existing→new" means new adds nothing beyond what existing already says.
- "complement": both lessons are individually true and independently useful; one adds detail, scope, a different aspect, or a further step on top of the other rather than replacing it. direction=null.
- "contradicts": the lessons give opposite advice for the same situation without clearly superseding. direction=null.
- "unrelated": different topics or only superficially similar.

Choosing between "supersedes" and "complement": ask "if someone followed ONLY the older lesson, would they be wrong?" If wrong → supersedes. If merely less informed → complement. Topical overlap alone is NEVER supersession; lessons about the same tool, project, or subsystem usually complement each other.

Be conservative: if in doubt between supersedes and complement, prefer "complement"; if in doubt overall, prefer "unrelated" with low confidence.
Output ONLY the JSON object — no prose, no code fences.
"""


# Words that describe ADDING to a lesson rather than replacing it. When the
# judge labels a pair 'supersedes' but its own reasoning uses one of these, the
# label contradicts the explanation and the explanation is the reliable half.
#
# Derived from the 2026-09-12 queue review: of 19 pending proposals, 16 were
# rejected and at least 5 announced themselves with exactly these phrases while
# still labelled 'supersedes' at ~0.92 confidence. See lessons #1327 and #2049.
#
# Deliberately excludes 'adds nothing' and similar, which describe the
# existing→new direction where the new lesson really is redundant.
_ADDITIVE_TELLS = (
    r"\badds?\b(?!\s+nothing)",
    r"\badditional\b",
    r"\balso\s+(?:covers|adds|includes|documents)\b",
    r"\bextends?\b",
    r"\bexpands?\b",
    r"\bgeneral[iz]s?es\b",
    r"\bgeneralizes\b",
    r"\bcompanion\b",
    r"\bbeyond\s+(?:what|the)\b",
    r"\bdoes\s+not\s+(?:address|cover|mention)\b",
    r"\bdo\s+not\s+address\b",
)
# NOT included, deliberately: "more complete". Backtested over all 68 decided
# queue entries it split 1 rejected / 1 approved — a coin flip, so it adds
# noise without adding signal. Re-measure before adding any new tell.

_ADDITIVE_RE = re.compile("|".join(_ADDITIVE_TELLS), re.IGNORECASE)

_DEMOTION_MARKER = "[auto-demoted supersedes→complement: reasoning describes addition, not obsolescence]"


def demote_additive_supersede(verdict: JudgeVerdict) -> JudgeVerdict:
    """Reclassify a 'supersedes' whose own reasoning describes addition.

    A true supersede means following the OLD lesson would now be wrong. When
    the judge's stated reason is that the new lesson *adds* something the old
    one lacks, both lessons are individually true and the old one must not be
    retired — that is 'complement' by the taxonomy's own definition.

    Returns the verdict unchanged unless it is a 'supersedes' carrying an
    additive tell. Idempotent: an already-demoted verdict is a 'complement'
    and is returned untouched.
    """
    if verdict.relationship != "supersedes":
        return verdict
    if not _ADDITIVE_RE.search(verdict.reasoning or ""):
        return verdict
    return JudgeVerdict(
        relationship="complement",
        direction=None,
        confidence=verdict.confidence,
        reasoning=f"{_DEMOTION_MARKER} {verdict.reasoning}",
    )


def _build_user_prompt(new_title: str, new_content: str,
                       cand_title: str, cand_content: str) -> str:
    return (
        f"NEW LESSON\nTitle: {new_title}\nContent: {new_content}\n\n"
        f"EXISTING LESSON\nTitle: {cand_title}\nContent: {cand_content}"
    )


def _parse(text: str) -> JudgeVerdict | None:
    """Parse judge output. Returns None on any parse failure.

    Handles ```json ... ``` fenced output that Haiku often emits despite
    the system prompt asking for bare JSON.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Drop opening ``` (or ```json) and closing ```
        if lines[0].startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    try:
        data = json.loads(text.strip())
        rel = data["relationship"]
        if rel not in ("duplicate", "supersedes", "complement", "contradicts", "unrelated"):
            return None
        direction = data.get("direction")
        if direction not in (None, "new→existing", "existing→new"):
            return None
        if rel == "supersedes" and direction is None:
            return None
        confidence = float(data["confidence"])
        if not 0.0 <= confidence <= 1.0:
            return None
        reasoning = str(data.get("reasoning") or "(no reasoning provided)")
        return JudgeVerdict(relationship=rel, direction=direction,
                            confidence=confidence, reasoning=reasoning)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


async def adjudicate(
    client: AsyncAnthropic,
    new_title: str,
    new_content: str,
    candidate_title: str,
    candidate_content: str,
    model: str,
    timeout: float,
) -> JudgeVerdict:
    """Classify a candidate pair. On timeout or parse failure, return unrelated@0.0."""
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=300,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_prompt(
                    new_title, new_content, candidate_title, candidate_content)}],
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return JudgeVerdict("unrelated", None, 0.0, "judge timeout")
    except Exception as e:
        logger.warning("judge call failed: %s", e)
        return JudgeVerdict("unrelated", None, 0.0, f"judge error: {type(e).__name__}")

    text = "".join(block.text for block in resp.content if hasattr(block, "text"))
    verdict = _parse(text)
    if verdict is None:
        return JudgeVerdict("unrelated", None, 0.0, "judge output unparseable")
    return verdict
