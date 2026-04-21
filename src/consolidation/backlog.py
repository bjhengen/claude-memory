"""Backlog analysis helpers: pair generation, judge-and-record, report rendering."""

import logging
from collections import Counter
from typing import Any

import asyncpg
from anthropic import AsyncAnthropic

from src.consolidation.judge import adjudicate

logger = logging.getLogger(__name__)


async def generate_pairs(
    pool: asyncpg.Pool,
    cosine_threshold: float,
) -> list[dict[str, Any]]:
    """
    Return every unique live-lesson pair with pairwise cosine >= threshold.

    Canonical ordering: lesson_a_id < lesson_b_id. One row per unordered pair.
    Both lessons must have embeddings and be non-retired.

    Returns rows sorted by cosine descending so --limit runs judge the
    highest-signal pairs first.
    """
    rows = await pool.fetch(
        """
        SELECT a.id AS lesson_a_id, b.id AS lesson_b_id,
               (1 - (a.embedding <=> b.embedding)) AS cosine,
               a.title AS a_title, a.content AS a_content,
               b.title AS b_title, b.content AS b_content
        FROM lessons a
        JOIN lessons b ON a.id < b.id
        WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND a.retired_at IS NULL AND b.retired_at IS NULL
          AND (1 - (a.embedding <=> b.embedding)) >= $1
        ORDER BY cosine DESC
        """,
        cosine_threshold,
    )
    return [dict(r) for r in rows]


async def judge_and_record(
    pool: asyncpg.Pool,
    anthropic: AsyncAnthropic,
    pair: dict[str, Any],
    batch_run_id: str,
    judge_model: str,
    timeout: float,
) -> None:
    """
    Call the v5 judge for one pair and write the verdict to backlog_analysis.

    Idempotent: if the (batch_run_id, a, b) row already exists, the INSERT
    is a no-op via ON CONFLICT DO NOTHING. Errors in the judge call are
    recorded as verdict=unrelated, confidence=0.0 (same fallback as v5).
    """
    verdict = await adjudicate(
        anthropic,
        new_title=pair["a_title"], new_content=pair["a_content"],
        candidate_title=pair["b_title"], candidate_content=pair["b_content"],
        model=judge_model, timeout=timeout,
    )
    try:
        await pool.execute(
            """
            INSERT INTO backlog_analysis
              (batch_run_id, lesson_a_id, lesson_b_id, cosine_similarity,
               judge_model, verdict, direction, confidence, reasoning)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (batch_run_id, lesson_a_id, lesson_b_id) DO NOTHING
            """,
            batch_run_id, pair["lesson_a_id"], pair["lesson_b_id"],
            float(pair["cosine"]), judge_model, verdict.relationship,
            verdict.direction, verdict.confidence, verdict.reasoning,
        )
    except Exception as e:
        logger.warning(
            "backlog insert failed for pair (%s,%s): %s",
            pair["lesson_a_id"], pair["lesson_b_id"], e,
        )


def render_report(rows: list[dict[str, Any]], thresholds) -> tuple[str, dict[str, Any]]:
    """
    Turn a list of backlog_analysis rows into a human-readable markdown report
    and a machine-readable JSON-safe dict.

    `thresholds` is any object with:
      - AUTO_MERGE_CONFIDENCE (float)
      - AUTO_SUPERSEDE_CONFIDENCE (float)
      - QUEUE_MIN_CONFIDENCE (float)
    Passing `src.consolidation.config` module here keeps the report reflecting
    whatever v5 thresholds are currently in effect.
    """
    total = len(rows)
    verdict_counts = Counter(r["verdict"] for r in rows)

    # Confidence histogram per verdict (buckets of 0.1)
    histogram: dict[str, dict[str, int]] = {}
    for verdict in ("duplicate", "supersedes", "contradicts", "unrelated"):
        buckets: Counter = Counter()
        for r in rows:
            if r["verdict"] != verdict:
                continue
            c = float(r["confidence"])
            bucket = f"{int(c * 10) / 10:.1f}-{(int(c * 10) + 1) / 10:.1f}"
            buckets[bucket] += 1
        histogram[verdict] = dict(sorted(buckets.items()))

    # Threshold-crossing counts — what v5 would do at current thresholds
    crossings = {"auto_merge": 0, "auto_supersede": 0, "enqueue": 0,
                 "flag_conflict": 0, "ignore": 0}
    for r in rows:
        v = r["verdict"]
        c = float(r["confidence"])
        if v == "duplicate":
            if c >= thresholds.AUTO_MERGE_CONFIDENCE:
                crossings["auto_merge"] += 1
            elif c >= thresholds.QUEUE_MIN_CONFIDENCE:
                crossings["enqueue"] += 1
            else:
                crossings["ignore"] += 1
        elif v == "supersedes":
            if c >= thresholds.AUTO_SUPERSEDE_CONFIDENCE:
                crossings["auto_supersede"] += 1
            elif c >= thresholds.QUEUE_MIN_CONFIDENCE:
                crossings["enqueue"] += 1
            else:
                crossings["ignore"] += 1
        elif v == "contradicts":
            if c >= thresholds.QUEUE_MIN_CONFIDENCE:
                crossings["flag_conflict"] += 1
            else:
                crossings["ignore"] += 1
        else:  # unrelated
            crossings["ignore"] += 1

    # Top-20 highest-confidence pairs (excluding unrelated)
    top20 = sorted(
        [r for r in rows if r["verdict"] != "unrelated"],
        key=lambda r: float(r["confidence"]),
        reverse=True,
    )[:20]

    data = {
        "total_pairs": total,
        "verdict_counts": dict(verdict_counts),
        "confidence_histogram": histogram,
        "threshold_crossings": crossings,
        "top_20": [
            {
                "lesson_a_id": r["lesson_a_id"], "lesson_b_id": r["lesson_b_id"],
                "a_title": r.get("a_title"), "b_title": r.get("b_title"),
                "cosine": float(r["cosine_similarity"]),
                "verdict": r["verdict"], "direction": r.get("direction"),
                "confidence": float(r["confidence"]),
                "reasoning": r["reasoning"],
            }
            for r in top20
        ],
    }

    # Markdown rendering
    lines = ["# Backlog Analysis Report", ""]
    lines.append(f"**Total pairs judged:** {total}")
    lines.append("")
    lines.append("## Verdict distribution")
    lines.append("")
    for v in ("duplicate", "supersedes", "contradicts", "unrelated"):
        n = verdict_counts.get(v, 0)
        pct = (n / total * 100) if total else 0
        lines.append(f"- **{v}**: {n} ({pct:.1f}%)")
    lines.append("")
    lines.append("## Confidence histogram")
    lines.append("")
    for v in ("duplicate", "supersedes", "contradicts", "unrelated"):
        if not histogram.get(v):
            continue
        lines.append(f"### {v}")
        for bucket, n in histogram[v].items():
            lines.append(f"- {bucket}: {n}")
        lines.append("")
    lines.append("## Threshold crossings (at current v5 thresholds)")
    lines.append("")
    lines.append(f"- **auto_merge**: {crossings['auto_merge']}")
    lines.append(f"- **auto_supersede**: {crossings['auto_supersede']}")
    lines.append(f"- **enqueue**: {crossings['enqueue']}")
    lines.append(f"- **flag_conflict**: {crossings['flag_conflict']}")
    lines.append(f"- **ignore**: {crossings['ignore']}")
    lines.append("")
    lines.append("## Top-20 highest-confidence pairs")
    lines.append("")
    for i, r in enumerate(data["top_20"], 1):
        lines.append(
            f"{i}. [{r['verdict']}@{r['confidence']:.2f}, cos={r['cosine']:.3f}] "
            f"#{r['lesson_a_id']} ↔ #{r['lesson_b_id']}: {r['reasoning']}"
        )

    return "\n".join(lines), data
