# V5.1 Backlog Analysis — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a one-shot analytical pass that runs every live-lesson pair above cosine 0.85 through the v5 judge and records verdicts in a new `backlog_analysis` table. No merges, no retirements — measurement only.

**Architecture:** One new table (`backlog_analysis`), one helper module (`src/consolidation/backlog.py`) with three pure-ish functions (`generate_pairs`, `judge_and_record`, `render_report`), and two CLI scripts (`scripts/analyze_backlog.py`, `scripts/backlog_report.py`). Reuses v5's `adjudicate()` judge and `src.consolidation.config` thresholds. Strictly additive — no changes to v5 code, tables, or behavior.

**Tech Stack:** PostgreSQL 16 (pgvector, asyncpg), Python 3.11, Anthropic SDK (existing), pytest + pytest-asyncio (existing).

**Design doc:** `docs/plans/2026-04-20-v5-1-backlog-analysis-design.md`

**Dev environment:** same slmbeast test-DB workflow as v5 (rsync to `slmbeast:~/dev/claude-memory/`, run tests via `ssh slmbeast 'cd ~/dev/claude-memory && source venv/bin/activate && pytest ...'`). Test DB at `postgresql://claude:claude@slmbeast:5434/claude_memory_test` already has the v5 schema applied.

**Branching:** All work on a new feature branch `v5-1-backlog-analysis`, merged fast-forward to main at completion.

---

### Task 1: Create Feature Branch + Migration File

**Files:**
- Create: `db/migrations/v5_1_backlog_analysis.sql`

- [ ] **Step 1: Create branch from main**

Run: `git checkout -b v5-1-backlog-analysis`
Expected: "Switched to a new branch 'v5-1-backlog-analysis'"

- [ ] **Step 2: Write the migration file**

Create `db/migrations/v5_1_backlog_analysis.sql` with:

```sql
-- =============================================================================
-- Migration: v5_1_backlog_analysis.sql
-- Date: 2026-04-20
-- Purpose: Add backlog analysis table for one-shot measurement pass
--   - Records verdicts for every live-lesson pair above cosine threshold
--   - Read-only use: no merges, no retirements are driven by this data
--   - Idempotent and resumable via UNIQUE(batch_run_id, a_id, b_id)
-- Idempotent: safe to run multiple times
-- =============================================================================

CREATE TABLE IF NOT EXISTS backlog_analysis (
    id SERIAL PRIMARY KEY,
    batch_run_id VARCHAR(100) NOT NULL,
    lesson_a_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    lesson_b_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    cosine_similarity NUMERIC(4,3) NOT NULL,
    judge_model VARCHAR(50) NOT NULL,
    verdict VARCHAR(20) NOT NULL CHECK (verdict IN ('duplicate','supersedes','contradicts','unrelated')),
    direction VARCHAR(30),  -- only set when verdict='supersedes'
    confidence NUMERIC(3,2) NOT NULL,
    reasoning TEXT NOT NULL,
    judged_at TIMESTAMP DEFAULT NOW(),
    CHECK (lesson_a_id < lesson_b_id),
    UNIQUE(batch_run_id, lesson_a_id, lesson_b_id)
);

CREATE INDEX IF NOT EXISTS idx_backlog_analysis_batch
    ON backlog_analysis(batch_run_id);
CREATE INDEX IF NOT EXISTS idx_backlog_analysis_verdict
    ON backlog_analysis(verdict, confidence DESC);
```

- [ ] **Step 3: Apply migration to the slmbeast test DB**

Run:
```bash
rsync -av ~/dev/claude-memory/db/migrations/v5_1_backlog_analysis.sql slmbeast:~/dev/claude-memory/db/migrations/v5_1_backlog_analysis.sql
ssh slmbeast "docker cp ~/dev/claude-memory/db/migrations/v5_1_backlog_analysis.sql claude_memory_test_db:/tmp/v5_1.sql && docker exec claude_memory_test_db psql -U claude -d claude_memory_test -f /tmp/v5_1.sql"
```

Expected: `CREATE TABLE` + two `CREATE INDEX` messages, no errors.

- [ ] **Step 4: Verify table exists**

Run:
```bash
ssh slmbeast "docker exec claude_memory_test_db psql -U claude -d claude_memory_test -c '\d backlog_analysis'"
```

Expected: table schema printed showing all 11 columns.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/v5_1_backlog_analysis.sql
git commit -m "feat(db): add v5.1 backlog_analysis table (measurement-only)"
```

---

### Task 2: Pair Generator — TDD

**Files:**
- Create: `src/consolidation/backlog.py`
- Create: `tests/test_backlog_pairs.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_backlog_pairs.py`:

```python
"""Tests for the backlog pair generator."""

import pytest

from src.consolidation.backlog import generate_pairs


async def _insert_lesson(conn, title, content, embedding):
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
    row = await conn.fetchrow(
        "INSERT INTO lessons (title, content, embedding) "
        "VALUES ($1, $2, $3::vector) RETURNING id",
        title, content, emb_str,
    )
    return row["id"]


@pytest.mark.asyncio
async def test_generate_pairs_returns_only_above_threshold(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM lessons WHERE title LIKE 'T\\_BP\\_%' ESCAPE '\\'"
        )
        # Two near-duplicates (cosine ≈ 1.0)
        a = await _insert_lesson(conn, "T_BP_A", "close one", [0.1] * 1536)
        b = await _insert_lesson(conn, "T_BP_B", "close two", [0.1] * 1536)
        # One far neighbor (cosine much lower when paired with A or B)
        far_emb = [0.9] * 1536
        c = await _insert_lesson(conn, "T_BP_C", "far one", far_emb)
        # One retired lesson (must NOT appear in results)
        await conn.execute(
            "INSERT INTO lessons (title, content, embedding, retired_at) "
            "VALUES ($1, $2, $3::vector, NOW())",
            "T_BP_RETIRED", "retired", "[" + ",".join(["0.1"] * 1536) + "]"
        )

    pairs = await generate_pairs(db_pool, cosine_threshold=0.95)
    titles = {(p["a_title"], p["b_title"]) for p in pairs
              if p["a_title"].startswith("T_BP_") and p["b_title"].startswith("T_BP_")}

    # The (A,B) pair is well above 0.95 cosine; (A,C), (B,C) are below; retired excluded
    assert ("T_BP_A", "T_BP_B") in titles
    assert not any("T_BP_C" in pair for pair in titles)
    assert not any("T_BP_RETIRED" in pair for pair in titles)

    # Assert canonical ordering: lesson_a_id < lesson_b_id for every pair
    for p in pairs:
        assert p["lesson_a_id"] < p["lesson_b_id"]
```

- [ ] **Step 2: Sync to slmbeast and run the test (expect FAIL)**

```bash
rsync -av ~/dev/claude-memory/tests/test_backlog_pairs.py slmbeast:~/dev/claude-memory/tests/test_backlog_pairs.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_backlog_pairs.py -v"
```

Expected: 1 FAILED with ImportError — `src.consolidation.backlog` doesn't exist yet.

- [ ] **Step 3: Implement `generate_pairs`**

Create `src/consolidation/backlog.py`:

```python
"""Backlog analysis helpers: pair generation, judge-and-record, report rendering."""

from typing import Any

import asyncpg


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
```

- [ ] **Step 4: Sync and re-run the test (expect PASS)**

```bash
rsync -av ~/dev/claude-memory/src/consolidation/backlog.py slmbeast:~/dev/claude-memory/src/consolidation/backlog.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_backlog_pairs.py -v"
```

Expected: 1 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/consolidation/backlog.py tests/test_backlog_pairs.py
git commit -m "feat(v5.1): add pair generator for backlog analysis"
```

---

### Task 3: Judge-and-Record Helper

**Files:**
- Modify: `src/consolidation/backlog.py`

No unit test: this function is thin glue over `adjudicate()` and an INSERT. Validated via manual smoke in Task 6.

- [ ] **Step 1: Append `judge_and_record` to `src/consolidation/backlog.py`**

Append at the bottom:

```python
import logging

from anthropic import AsyncAnthropic

from src.consolidation.judge import adjudicate

logger = logging.getLogger(__name__)


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
```

- [ ] **Step 2: Sync and verify import**

```bash
rsync -av ~/dev/claude-memory/src/consolidation/backlog.py slmbeast:~/dev/claude-memory/src/consolidation/backlog.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && python -c 'from src.consolidation.backlog import judge_and_record; print(\"ok\")'"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/consolidation/backlog.py
git commit -m "feat(v5.1): add judge_and_record backlog helper"
```

---

### Task 4: Report Renderer — TDD

**Files:**
- Modify: `src/consolidation/backlog.py`
- Create: `tests/test_backlog_report.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_backlog_report.py`:

```python
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
```

- [ ] **Step 2: Sync and run the test (expect FAIL)**

```bash
rsync -av ~/dev/claude-memory/tests/test_backlog_report.py slmbeast:~/dev/claude-memory/tests/test_backlog_report.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_backlog_report.py -v"
```

Expected: 3 FAILED with ImportError — `render_report` doesn't exist.

- [ ] **Step 3: Append `render_report` to `src/consolidation/backlog.py`**

Append at the bottom:

```python
from collections import Counter


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
        buckets = Counter()
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
```

- [ ] **Step 4: Sync and re-run the test (expect PASS)**

```bash
rsync -av ~/dev/claude-memory/src/consolidation/backlog.py slmbeast:~/dev/claude-memory/src/consolidation/backlog.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_backlog_report.py -v"
```

Expected: 3 PASSED.

- [ ] **Step 5: Run all tests to confirm no regressions**

```bash
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/ -v 2>&1 | tail -20"
```

Expected: all prior tests still pass; the two new tests (pairs, report) also pass. Total 23+ passed.

- [ ] **Step 6: Commit**

```bash
git add src/consolidation/backlog.py tests/test_backlog_report.py
git commit -m "feat(v5.1): add render_report with verdict/threshold stats"
```

---

### Task 5: Analyze-Backlog CLI Script

**Files:**
- Create: `scripts/analyze_backlog.py`

- [ ] **Step 1: Write the script**

Create `scripts/analyze_backlog.py`:

```python
#!/usr/bin/env python3
"""
v5.1 backlog analyzer — one-shot pass over every above-threshold lesson pair.

Connects to the DB via DATABASE_URL, enumerates all live-live pairs with
cosine >= threshold, judges each via Claude Haiku, and writes results to
the `backlog_analysis` table. Resumable by re-invoking with the same batch-id.

Usage:
    python -m scripts.analyze_backlog --batch-id pilot-YYYY-MM-DD [options]

Options:
    --cosine-threshold  float   Minimum pairwise cosine (default 0.85)
    --concurrency       int     Max in-flight Anthropic calls (default 10)
    --limit             int     Process only the first N remaining pairs
    --dry-run                   Count pairs and print plan; no Anthropic calls
"""

import argparse
import asyncio
import logging
import os
import sys

import asyncpg
from anthropic import AsyncAnthropic

from src.consolidation import config
from src.consolidation.backlog import generate_pairs, judge_and_record

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("analyze_backlog")


async def _already_judged_pairs(pool, batch_run_id):
    rows = await pool.fetch(
        "SELECT lesson_a_id, lesson_b_id FROM backlog_analysis WHERE batch_run_id=$1",
        batch_run_id,
    )
    return {(r["lesson_a_id"], r["lesson_b_id"]) for r in rows}


async def _worker(sem, pool, anthropic, pair, batch_run_id, model, timeout, counter, total):
    async with sem:
        await judge_and_record(pool, anthropic, pair, batch_run_id, model, timeout)
        counter["done"] += 1
        if counter["done"] % 25 == 0:
            logger.info(
                "[%d/%d] pair #%s↔#%s cosine=%.3f",
                counter["done"], total,
                pair["lesson_a_id"], pair["lesson_b_id"], pair["cosine"],
            )


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", required=True)
    p.add_argument("--cosine-threshold", type=float, default=0.85)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL env var is required")
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY") and not args.dry_run:
        logger.error("ANTHROPIC_API_KEY env var is required (unless --dry-run)")
        return 2

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=5)
    try:
        pairs = await generate_pairs(pool, cosine_threshold=args.cosine_threshold)
        total_all = len(pairs)
        logger.info(
            "total pairs above cosine %.2f: %d", args.cosine_threshold, total_all,
        )

        already = await _already_judged_pairs(pool, args.batch_id)
        remaining = [
            pr for pr in pairs
            if (pr["lesson_a_id"], pr["lesson_b_id"]) not in already
        ]
        logger.info(
            "resume state: %d already judged, %d remaining for batch %r",
            len(already), len(remaining), args.batch_id,
        )

        if args.limit is not None:
            remaining = remaining[: args.limit]
            logger.info("--limit %d active; processing first %d", args.limit, len(remaining))

        if args.dry_run:
            logger.info("DRY RUN — no Anthropic calls will be made; exiting.")
            return 0

        if not remaining:
            logger.info("nothing to do; batch is complete.")
            return 0

        anthropic = AsyncAnthropic()
        sem = asyncio.Semaphore(args.concurrency)
        counter = {"done": 0}

        tasks = [
            asyncio.create_task(_worker(
                sem, pool, anthropic, pr, args.batch_id,
                config.JUDGE_MODEL, config.JUDGE_TIMEOUT_SECONDS,
                counter, len(remaining),
            ))
            for pr in remaining
        ]
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            logger.warning("interrupted — waiting for in-flight pairs to settle")
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("complete — %d pairs processed this run", counter["done"])
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Sync and verify it imports and runs --dry-run against slmbeast test DB**

```bash
rsync -av ~/dev/claude-memory/scripts/analyze_backlog.py slmbeast:~/dev/claude-memory/scripts/analyze_backlog.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && PYTHONPATH=/home/bhengen/dev/claude-memory DATABASE_URL=postgresql://claude:claude@localhost:5434/claude_memory_test python -m scripts.analyze_backlog --batch-id test-dryrun --dry-run"
```

Expected: logs show total pair count for the test DB's lessons (small number — test DB has <10 rows), logs "DRY RUN — no Anthropic calls will be made", exit code 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_backlog.py
git commit -m "feat(v5.1): add analyze_backlog.py CLI script"
```

---

### Task 6: Small Live-Judge Smoke on Test DB

**Files:** none

Run the analyzer for real (small scale) on the slmbeast test DB to exercise the full pipeline before pointing it at prod.

- [ ] **Step 1: Seed two near-duplicate lessons in the test DB**

```bash
ssh slmbeast "docker exec claude_memory_test_db psql -U claude -d claude_memory_test -c \"DELETE FROM lessons WHERE title LIKE 'V51%';\""
ssh slmbeast "docker exec claude_memory_test_db psql -U claude -d claude_memory_test -c \"
INSERT INTO lessons (title, content, embedding) VALUES
  ('V51_A', 'iOS share sheet requires sharePositionOrigin on all iOS devices.',
   ('[' || (SELECT string_agg('0.12', ',') FROM generate_series(1,1536)) || ']')::vector(1536)),
  ('V51_B', 'Always set sharePositionOrigin for every iOS device.',
   ('[' || (SELECT string_agg('0.12', ',') FROM generate_series(1,1536)) || ']')::vector(1536));
\""
```

Expected: `INSERT 0 2` (plus the `DELETE` message).

Note on the embedding literal: we want both lessons to have identical uniform embeddings (cosine = 1.0) so `generate_pairs` at threshold 0.85 finds them. The `'[' || ... || ']'` wrapping produces the bracketed pgvector literal (e.g. `'[0.12,0.12,...,0.12]'`) that `::vector(1536)` accepts.

- [ ] **Step 2: Run the analyzer with a small limit**

Supply your Anthropic API key via the local shell; it's then forwarded via the ssh command:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."  # supply the key once, in local shell
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && PYTHONPATH=/home/bhengen/dev/claude-memory DATABASE_URL=postgresql://claude:claude@localhost:5434/claude_memory_test ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY python -m scripts.analyze_backlog --batch-id smoke-v51 --limit 5"
```

Expected output: at least 1 pair processed (V51_A × V51_B), log line showing verdict and cosine.

Acceptance: query the test DB afterward —

```bash
ssh slmbeast "docker exec claude_memory_test_db psql -U claude -d claude_memory_test -c 'SELECT batch_run_id, lesson_a_id, lesson_b_id, verdict, confidence FROM backlog_analysis ORDER BY id DESC LIMIT 5;'"
```

Expected: one row, verdict=duplicate, confidence in [0.85, 1.00].

- [ ] **Step 3: Re-run the analyzer to validate resumability**

Same command as Step 2. Expected log line: `nothing to do; batch is complete.` (the pair is already in `backlog_analysis` for this `batch_run_id`).

- [ ] **Step 4: No commit — smoke test only.**

---

### Task 7: Report CLI Script

**Files:**
- Create: `scripts/backlog_report.py`

- [ ] **Step 1: Write the script**

Create `scripts/backlog_report.py`:

```python
#!/usr/bin/env python3
"""
v5.1 backlog report — render the backlog_analysis table as markdown or JSON.

Usage:
    python -m scripts.backlog_report --batch-id pilot-YYYY-MM-DD [options]

Options:
    --format  {markdown,json,both}   Output format (default markdown)
    --output  PATH                    Write to file instead of stdout
"""

import argparse
import asyncio
import json
import logging
import os
import sys

import asyncpg

from src.consolidation import config
from src.consolidation.backlog import render_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backlog_report")


async def _fetch_rows(pool, batch_run_id):
    rows = await pool.fetch(
        """
        SELECT ba.id, ba.batch_run_id, ba.lesson_a_id, ba.lesson_b_id,
               ba.cosine_similarity, ba.judge_model, ba.verdict, ba.direction,
               ba.confidence, ba.reasoning, ba.judged_at,
               la.title AS a_title, lb.title AS b_title
        FROM backlog_analysis ba
        JOIN lessons la ON la.id = ba.lesson_a_id
        JOIN lessons lb ON lb.id = ba.lesson_b_id
        WHERE ba.batch_run_id = $1
        ORDER BY ba.confidence DESC
        """,
        batch_run_id,
    )
    return [dict(r) for r in rows]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", required=True)
    p.add_argument("--format", choices=("markdown", "json", "both"), default="markdown")
    p.add_argument("--output", default=None)
    args = p.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL env var is required")
        return 2

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    try:
        rows = await _fetch_rows(pool, args.batch_id)
        if not rows:
            logger.warning("no rows found for batch_run_id=%r", args.batch_id)
            return 1

        md, data = render_report(rows, config)

        if args.format == "markdown":
            output = md
        elif args.format == "json":
            output = json.dumps(data, indent=2, default=str)
        else:  # both
            output = md + "\n\n---\n\n```json\n" + json.dumps(data, indent=2, default=str) + "\n```"

        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            logger.info("wrote %d chars to %s", len(output), args.output)
        else:
            print(output)

        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Sync and exercise it against the test DB's smoke batch**

```bash
rsync -av ~/dev/claude-memory/scripts/backlog_report.py slmbeast:~/dev/claude-memory/scripts/backlog_report.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && PYTHONPATH=/home/bhengen/dev/claude-memory DATABASE_URL=postgresql://claude:claude@localhost:5434/claude_memory_test python -m scripts.backlog_report --batch-id smoke-v51"
```

Expected: a small markdown report showing 1 pair total, 1 duplicate, 1 auto_merge in threshold crossings.

- [ ] **Step 3: Commit**

```bash
git add scripts/backlog_report.py
git commit -m "feat(v5.1): add backlog_report.py CLI script"
```

---

### Task 8: Deploy Migration to Production

**Files:** none (deployment step)

- [ ] **Step 1: Copy the migration file to EC2**

```bash
scp -i ~/.ssh/AWS_FR.pem ~/dev/claude-memory/db/migrations/v5_1_backlog_analysis.sql ubuntu@44.212.169.119:~/claude-memory/db/migrations/v5_1_backlog_analysis.sql
```

Expected: file transferred, no errors.

- [ ] **Step 2: Apply to the prod DB**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker cp ~/claude-memory/db/migrations/v5_1_backlog_analysis.sql claude_memory_db:/tmp/v5_1.sql && docker exec claude_memory_db psql -U claude -d claude_memory -f /tmp/v5_1.sql"
```

Expected: `CREATE TABLE` + 2 `CREATE INDEX` messages, no errors.

- [ ] **Step 3: Verify table exists in prod**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_db psql -U claude -d claude_memory -c '\d backlog_analysis'"
```

Expected: full schema of the new table.

- [ ] **Step 4: No commit — deployment only.**

---

### Task 9: Dry-Run Against Production

**Files:** none

- [ ] **Step 1: Copy the source code onto EC2 (needed for the script to run there)**

```bash
scp -i ~/.ssh/AWS_FR.pem ~/dev/claude-memory/src/consolidation/backlog.py ubuntu@44.212.169.119:~/claude-memory/src/consolidation/backlog.py
scp -i ~/.ssh/AWS_FR.pem -r ~/dev/claude-memory/scripts ubuntu@44.212.169.119:~/claude-memory/
```

Expected: files transferred.

- [ ] **Step 2: Run the analyzer dry-run inside the prod MCP container**

The `claude_memory_mcp` container already has the Python environment, anthropic SDK, and DB connectivity. Easiest to run the script there.

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker cp ~/claude-memory/src/consolidation/backlog.py claude_memory_mcp:/app/src/consolidation/backlog.py && docker cp ~/claude-memory/scripts claude_memory_mcp:/app/scripts && docker exec claude_memory_mcp python -m scripts.analyze_backlog --batch-id pilot-2026-04-21 --dry-run"
```

Expected log lines:
```
... total pairs above cosine 0.85: N
... resume state: 0 already judged, N remaining for batch 'pilot-2026-04-21'
... DRY RUN — no Anthropic calls will be made; exiting.
```

Record the value of N. This is the number of pairs the full run will judge.

- [ ] **Step 3: Decide whether to proceed**

If N is in the expected range (~500–2000), continue to Task 10.
If N is much larger (e.g., >5000), re-run with a tighter `--cosine-threshold 0.88` or similar to reduce the pair count.

- [ ] **Step 4: No commit — validation only.**

---

### Task 10: Staged Run (--limit 50) on Production

**Files:** none

- [ ] **Step 1: Run 50 pairs**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_mcp python -m scripts.analyze_backlog --batch-id pilot-2026-04-21 --limit 50"
```

Expected: progress log lines every 25 pairs; total 50 pairs judged; exit code 0. Should take ~1 minute at concurrency=10.

- [ ] **Step 2: Sanity-check the rows that were written**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_db psql -U claude -d claude_memory -c \"SELECT verdict, COUNT(*), ROUND(AVG(confidence), 2) AS avg_conf FROM backlog_analysis WHERE batch_run_id='pilot-2026-04-21' GROUP BY verdict;\""
```

Expected: 4 rows (one per verdict; some may have count=0 — that's fine). No rows where `confidence=0.0` (would indicate judge errors on every call — smell test).

If `confidence=0.0` rows appear, investigate before the full run (check `ANTHROPIC_API_KEY` on the container, check docker logs for judge errors).

- [ ] **Step 3: Hand-inspect the top 5 pairs by confidence**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_db psql -U claude -d claude_memory -c \"SELECT ba.lesson_a_id, ba.lesson_b_id, ba.verdict, ba.confidence, ba.reasoning, la.title AS a_title, lb.title AS b_title FROM backlog_analysis ba JOIN lessons la ON la.id=ba.lesson_a_id JOIN lessons lb ON lb.id=ba.lesson_b_id WHERE ba.batch_run_id='pilot-2026-04-21' ORDER BY ba.confidence DESC LIMIT 5;\""
```

Acceptance: each of the top 5 either has a sensible `reasoning` string or the titles/contents clearly justify the verdict. If any of the top-5 reasoning strings are nonsensical, pause and investigate the judge behavior before committing to the full run.

- [ ] **Step 4: No commit — validation only.**

---

### Task 11: Full Run + Generate Report

**Files:** none

- [ ] **Step 1: Resume the batch to completion**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_mcp python -m scripts.analyze_backlog --batch-id pilot-2026-04-21"
```

Expected: resume logs show 50 already judged; remaining N-50 are processed. Total runtime in the 2–7 minute range for N≈500–2000. Exit code 0.

- [ ] **Step 2: Generate the report**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_mcp python -m scripts.backlog_report --batch-id pilot-2026-04-21 --format both" > /tmp/pilot-2026-04-21-report.md
cat /tmp/pilot-2026-04-21-report.md | head -50
```

Expected: markdown report printed to the file, first 50 lines show verdict distribution + start of histogram.

- [ ] **Step 3: Also dump JSON for later analysis**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_mcp python -m scripts.backlog_report --batch-id pilot-2026-04-21 --format json" > /tmp/pilot-2026-04-21-report.json
wc -l /tmp/pilot-2026-04-21-report.json
```

Expected: a multi-line JSON document.

- [ ] **Step 4: No commit — the report is a point-in-time artifact, not tracked in the repo.**

---

### Task 12: Merge to Main + Log Findings

**Files:** none (branch merge + memory)

- [ ] **Step 1: Verify all tests pass on feature branch**

```bash
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/ 2>&1 | tail -5"
```

Expected: all tests pass (previous 19 + 4 new ones from Tasks 2 and 4 = 23 total).

- [ ] **Step 2: Fast-forward merge to main**

```bash
git checkout main
git pull origin main
git merge --ff-only v5-1-backlog-analysis
git push origin main
```

Expected: push succeeds.

- [ ] **Step 3: Log a summary lesson with the findings**

Using the MCP `log_lesson` tool, log a lesson summarizing:
- Total pair count (N)
- Verdict distribution (duplicate / supersedes / contradicts / unrelated)
- Threshold-crossing counts (what v5 would have auto-merged, enqueued, flagged, ignored)
- Any surprises or threshold-adjustment recommendations

Title: `"V5.1 backlog analysis — corpus duplication findings (2026-04-21)"`.

Tags: `["v5-1", "backlog", "measurement", "thresholds"]`.

- [ ] **Step 4: Write a journal entry with a reflection on the findings**

Using the MCP `write_journal` tool, capture any qualitative observations about the corpus that don't fit the lesson format — e.g., what kinds of lessons tend to duplicate, whether the thresholds feel right, whether any unexpected contradictions showed up.

- [ ] **Step 5: No code commit — v5.1 is complete.**

---

## Rollback Plan

- **Migration:** `DROP TABLE backlog_analysis CASCADE;` — additive schema; nothing else depends on it.
- **Code:** `git revert <range>` if a defect is found post-merge. The code has no runtime effect on v5 log-time consolidation; reverting doesn't destabilize production.
- **Data:** the `backlog_analysis` rows are point-in-time measurements. Safe to delete rows for a specific `batch_run_id` with `DELETE FROM backlog_analysis WHERE batch_run_id = 'pilot-2026-04-21';` and re-run.

## What This Plan Does Not Cover

Per the design doc "What v5.1 Does Not Cover" section:

- No "apply" tool that converts `backlog_analysis` rows into actual merges/supersedes. Brian reviews the report first; apply tooling ships in a future iteration if wanted.
- No cross-batch comparison tooling.
- No embedding-model-change handling.
- No cross-entity consolidation (patterns, specs, agents).
