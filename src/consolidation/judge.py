"""LLM judge: classifies a candidate pair as duplicate / supersedes / contradicts / unrelated."""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


Relationship = Literal["duplicate", "supersedes", "contradicts", "unrelated"]
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
  "relationship": "duplicate" | "supersedes" | "contradicts" | "unrelated",
  "direction": "new→existing" | "existing→new" | null,
  "confidence": 0.0 to 1.0,
  "reasoning": "<one short sentence>"
}

Rules:
- "duplicate": both lessons convey substantively the same advice. direction=null.
- "supersedes": one lesson makes the other obsolete (e.g., a newer workaround replaces an older one). direction is required: "new→existing" means new supersedes existing; "existing→new" means existing already covers new's claim.
- "contradicts": the lessons give opposite advice for the same situation without clearly superseding. direction=null.
- "unrelated": different topics or only superficially similar.

Be conservative: if in doubt, prefer "unrelated" with low confidence.
Output ONLY the JSON object — no prose, no code fences.
"""


def _build_user_prompt(new_title: str, new_content: str,
                       cand_title: str, cand_content: str) -> str:
    return (
        f"NEW LESSON\nTitle: {new_title}\nContent: {new_content}\n\n"
        f"EXISTING LESSON\nTitle: {cand_title}\nContent: {cand_content}"
    )


def _parse(text: str) -> JudgeVerdict | None:
    """Parse judge output. Returns None on any parse failure."""
    try:
        data = json.loads(text.strip())
        rel = data["relationship"]
        if rel not in ("duplicate", "supersedes", "contradicts", "unrelated"):
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
