# V5.2 Backlog Batch Apply — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add one MCP tool (`apply_backlog_batch`) that converts the 82 high-confidence rows in `backlog_analysis` into real `lesson_merges` rows via v5's existing merge/supersede helpers. Preview-by-default, require explicit `confirm=true` and a `reviewer` to apply.

**Architecture:** One new file (`src/tools/backlog_apply.py`) with the MCP tool + two pure helpers (`_pick_canonical`, eligibility classification). One-line registration in `src/server.py`. Reuses v5's `execute_auto_merge` and `execute_auto_supersede`. Zero DB schema changes.

**Tech Stack:** Python 3.11, asyncpg, FastMCP, pytest (existing). No new deps.

**Design doc:** `docs/plans/2026-04-22-v5-2-backlog-apply-design.md` (e69653d)

**Dev environment:** Same rsync-to-slmbeast workflow as v5 / v5.1. Test DB at `postgresql://claude:claude@slmbeast:5434/claude_memory_test` already has the v5 + v5.1 schema.

**Branching:** All work on a new feature branch `v5-2-backlog-apply`, fast-forward merged to `main` at completion. Keep the branch as a marker (matches v5 and v5.1 convention).

---

### Task 1: Create Feature Branch

**Files:** none

- [ ] **Step 1: Create branch from main**

Run: `git checkout -b v5-2-backlog-apply`
Expected: `Switched to a new branch 'v5-2-backlog-apply'`

- [ ] **Step 2: No commit — branch setup only.**

---

### Task 2: `_pick_canonical` Helper — TDD

**Files:**
- Create: `src/tools/backlog_apply.py`
- Create: `tests/test_apply_canonical.py`

This is a pure DB-read helper used only for the `duplicate` verdict case, where neither side is "new." Rule: more upvotes wins → older `created_at` wins → lower id wins.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apply_canonical.py`:

```python
"""Tests for _pick_canonical — deterministic winner selection for duplicate merges."""

import pytest

from src.tools.backlog_apply import _pick_canonical


async def _insert_lesson(conn, title, upvotes=0, downvotes=0, created_at=None):
    emb_str = "[" + ",".join(["0.1"] * 1536) + "]"
    if created_at is None:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, downvotes) "
            "VALUES ($1, $2, $3::vector, $4, $5) RETURNING id",
            title, "x", emb_str, upvotes, downvotes,
        )
    else:
        row = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, downvotes, learned_at) "
            "VALUES ($1, $2, $3::vector, $4, $5, $6) RETURNING id",
            title, "x", emb_str, upvotes, downvotes, created_at,
        )
    return row["id"]


@pytest.mark.asyncio
async def test_pick_canonical_higher_upvotes_wins(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_%' ESCAPE '\\'")
        a = await _insert_lesson(conn, "T_PC_A", upvotes=5)
        b = await _insert_lesson(conn, "T_PC_B", upvotes=1)

        canonical, merged = await _pick_canonical(conn, a, b)
        assert canonical == a
        assert merged == b

        # Reversed input order — still picks A
        canonical, merged = await _pick_canonical(conn, b, a)
        assert canonical == a
        assert merged == b


@pytest.mark.asyncio
async def test_pick_canonical_older_wins_on_upvote_tie(db_pool):
    from datetime import datetime, timezone, timedelta
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_TIE\\_%' ESCAPE '\\'")
        earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        later = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        a = await _insert_lesson(conn, "T_PC_TIE_EARLY", upvotes=2, created_at=earlier)
        b = await _insert_lesson(conn, "T_PC_TIE_LATE", upvotes=2, created_at=later)

        canonical, merged = await _pick_canonical(conn, a, b)
        assert canonical == a  # older wins
        assert merged == b


@pytest.mark.asyncio
async def test_pick_canonical_lower_id_wins_on_full_tie(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_SAME\\_%' ESCAPE '\\'")
        # Insert both in one statement so timestamps are effectively equal
        a = await _insert_lesson(conn, "T_PC_SAME_A", upvotes=0)
        b = await _insert_lesson(conn, "T_PC_SAME_B", upvotes=0)

        canonical, merged = await _pick_canonical(conn, a, b)
        # a was inserted first → lower id → wins on final tiebreak
        assert canonical < merged
        assert {canonical, merged} == {a, b}


@pytest.mark.asyncio
async def test_pick_canonical_handles_null_learned_at(db_pool):
    """learned_at is nullable; NULL must not crash the Python sort."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_PC\\_NULL\\_%' ESCAPE '\\'")
        emb = "[" + ",".join(["0.1"] * 1536) + "]"
        row_a = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes, learned_at) "
            "VALUES ($1, $2, $3::vector, $4, NULL) RETURNING id",
            "T_PC_NULL_A", "x", emb, 0,
        )
        row_b = await conn.fetchrow(
            "INSERT INTO lessons (title, content, embedding, upvotes) "
            "VALUES ($1, $2, $3::vector, $4) RETURNING id",
            "T_PC_NULL_B", "x", emb, 0,
        )
        canonical, merged = await _pick_canonical(conn, row_a["id"], row_b["id"])
        # NULL learned_at sorts before any real timestamp via datetime.min coalesce,
        # so the NULL row wins (older). We just verify no TypeError.
        assert {canonical, merged} == {row_a["id"], row_b["id"]}
```

- [ ] **Step 2: Sync and run the tests (expect FAIL — module missing)**

```bash
rsync -av ~/dev/claude-memory/tests/test_apply_canonical.py slmbeast:~/dev/claude-memory/tests/test_apply_canonical.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_apply_canonical.py -v 2>&1 | tail -10"
```

Expected: ModuleNotFoundError for `src.tools.backlog_apply`.

- [ ] **Step 3: Create `src/tools/backlog_apply.py` with just `_pick_canonical`**

```python
"""v5.2 backlog batch-apply tool.

Converts high-confidence rows from backlog_analysis into real lesson_merges
entries via the existing v5 execute_auto_merge / execute_auto_supersede helpers.
"""

from typing import Any

import asyncpg


async def _pick_canonical(conn, a_id: int, b_id: int) -> tuple[int, int]:
    """
    Choose which lesson survives a duplicate merge when neither side is "new."

    Rule: higher upvotes wins → older learned_at wins → lower id wins.
    Returns (canonical_id, merged_id). canonical is the survivor.
    """
    from datetime import datetime

    rows = await conn.fetch(
        "SELECT id, COALESCE(upvotes, 0) AS upvotes, learned_at "
        "FROM lessons WHERE id = ANY($1)",
        [a_id, b_id],
    )
    if len(rows) != 2:
        raise ValueError(f"expected 2 lessons for ids ({a_id}, {b_id}), got {len(rows)}")

    # Sort ascending by sort_key; first element wins.
    # - upvotes: higher wins → negate
    # - learned_at: older wins → pass through; NULL coalesces to datetime.min
    #   (since learned_at is nullable, Python < would raise TypeError without coalesce)
    # - id: lower wins → pass through
    def sort_key(r):
        ts = r["learned_at"] or datetime.min
        return (-r["upvotes"], ts, r["id"])

    sorted_rows = sorted(rows, key=sort_key)
    canonical_id = sorted_rows[0]["id"]
    merged_id = sorted_rows[1]["id"]
    return canonical_id, merged_id
```

**Schema note:** The `lessons` table uses `learned_at` (not `created_at`) as the creation timestamp. This is verified by inspecting the prod schema dump from earlier sessions.

- [ ] **Step 4: Sync and re-run the tests (expect PASS)**

```bash
rsync -av ~/dev/claude-memory/src/tools/backlog_apply.py slmbeast:~/dev/claude-memory/src/tools/backlog_apply.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_apply_canonical.py -v 2>&1 | tail -10"
```

Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/tools/backlog_apply.py tests/test_apply_canonical.py
git commit -m "feat(v5.2): add _pick_canonical helper for duplicate merges"
```

---

### Task 3: Eligibility Filter — TDD

**Files:**
- Modify: `src/tools/backlog_apply.py`
- Create: `tests/test_apply_eligibility.py`

The eligibility filter takes the rows returned by the eligibility query and partitions them into `eligible` vs `skip` with reason codes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apply_eligibility.py`:

```python
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
```

- [ ] **Step 2: Sync and run the tests (expect FAIL)**

```bash
rsync -av ~/dev/claude-memory/tests/test_apply_eligibility.py slmbeast:~/dev/claude-memory/tests/test_apply_eligibility.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_apply_eligibility.py -v 2>&1 | tail -10"
```

Expected: `ImportError: cannot import name 'classify_eligibility' ...`

- [ ] **Step 3: Append `classify_eligibility` to `src/tools/backlog_apply.py`**

Append at the bottom of the file (after `_pick_canonical`):

```python
def classify_eligibility(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Partition rows into (eligible, skip).

    Skip reasons (checked in order; first match wins):
      - already_retired — either lesson has retired_at set
      - already_merged  — either lesson appears in lesson_merges (not reversed)
    """
    eligible = []
    skip = []
    for r in rows:
        if r["a_retired"] or r["b_retired"]:
            skip.append({**r, "reason": "already_retired"})
        elif r["a_in_merges"] or r["b_in_merges"]:
            skip.append({**r, "reason": "already_merged"})
        else:
            eligible.append(r)
    return eligible, skip
```

- [ ] **Step 4: Sync and re-run the tests (expect PASS)**

```bash
rsync -av ~/dev/claude-memory/src/tools/backlog_apply.py slmbeast:~/dev/claude-memory/src/tools/backlog_apply.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/test_apply_eligibility.py -v 2>&1 | tail -10"
```

Expected: 5 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/tools/backlog_apply.py tests/test_apply_eligibility.py
git commit -m "feat(v5.2): add classify_eligibility for backlog apply"
```

---

### Task 4: Add Eligibility Query Helper

**Files:**
- Modify: `src/tools/backlog_apply.py`

No unit test — thin wrapper around one SQL query; correctness is verified by the MCP tool's smoke test.

- [ ] **Step 1: Append `fetch_candidate_rows` to `src/tools/backlog_apply.py`**

Append at the bottom:

```python
async def fetch_candidate_rows(
    pool: asyncpg.Pool,
    batch_run_id: str,
    verdict_in: list[str],
    confidence_gte: float,
) -> list[dict[str, Any]]:
    """
    Return backlog_analysis rows matching filters, each annotated with:
      - a_retired, b_retired (bool): current retirement status
      - a_in_merges, b_in_merges (bool): participation in non-reversed merges
      - a_title, b_title: for report/preview display
    Sorted by confidence DESC.
    """
    rows = await pool.fetch(
        """
        WITH merged_lesson_ids AS (
          SELECT canonical_id AS lesson_id FROM lesson_merges WHERE reversed_at IS NULL
          UNION
          SELECT merged_id AS lesson_id FROM lesson_merges WHERE reversed_at IS NULL
        )
        SELECT
          ba.lesson_a_id, ba.lesson_b_id, ba.verdict, ba.direction,
          ba.confidence, ba.cosine_similarity, ba.reasoning, ba.judge_model,
          la.title AS a_title, (la.retired_at IS NOT NULL) AS a_retired,
          lb.title AS b_title, (lb.retired_at IS NOT NULL) AS b_retired,
          (la.id IN (SELECT lesson_id FROM merged_lesson_ids)) AS a_in_merges,
          (lb.id IN (SELECT lesson_id FROM merged_lesson_ids)) AS b_in_merges
        FROM backlog_analysis ba
        JOIN lessons la ON la.id = ba.lesson_a_id
        JOIN lessons lb ON lb.id = ba.lesson_b_id
        WHERE ba.batch_run_id = $1
          AND ba.verdict = ANY($2)
          AND ba.confidence >= $3
        ORDER BY ba.confidence DESC
        """,
        batch_run_id, verdict_in, confidence_gte,
    )
    return [dict(r) for r in rows]
```

- [ ] **Step 2: Sync and sanity-check that the import still works**

```bash
rsync -av ~/dev/claude-memory/src/tools/backlog_apply.py slmbeast:~/dev/claude-memory/src/tools/backlog_apply.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && python -c 'from src.tools.backlog_apply import fetch_candidate_rows, classify_eligibility, _pick_canonical; print(\"ok\")'"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/tools/backlog_apply.py
git commit -m "feat(v5.2): add fetch_candidate_rows eligibility query"
```

---

### Task 5: MCP Tool `apply_backlog_batch`

**Files:**
- Modify: `src/tools/backlog_apply.py`
- Modify: `src/server.py`

The MCP tool itself. Wraps preview + apply modes. No unit test — smoke-tested on prod.

- [ ] **Step 1: Append the MCP tool to `src/tools/backlog_apply.py`**

Append at the bottom:

```python
import json
import logging

from mcp.server.fastmcp import Context

from src.server import mcp
from src.consolidation.actor import execute_auto_merge, execute_auto_supersede
from src.consolidation.judge import JudgeVerdict

_logger = logging.getLogger(__name__)


@mcp.tool()
async def apply_backlog_batch(
    batch_run_id: str,
    confirm: bool = False,
    verdict_in: list[str] = None,
    confidence_gte: float = 0.90,
    max_apply: int = 50,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Apply high-confidence backlog_analysis rows as real lesson_merges.

    Preview-by-default: without confirm=true, returns a summary of what WOULD
    happen and writes nothing.

    Apply mode (confirm=true) requires a non-empty reviewer. Applies at most
    max_apply pairs in one call; re-call to process more.

    Args:
        batch_run_id: The batch_run_id from backlog_analysis (e.g., 'pilot-2026-04-21')
        confirm: If False (default), preview only — no DB writes
        verdict_in: Which verdict types to apply (default: ['duplicate', 'supersedes'])
        confidence_gte: Minimum confidence threshold (default 0.90)
        max_apply: Cap on pairs applied per call (default 50)
        reviewer: Required when confirm=true; recorded as decided_by in lesson_merges
    """
    if verdict_in is None:
        verdict_in = ["duplicate", "supersedes"]

    app = ctx.request_context.lifespan_context
    pool = app.db

    # Fetch + classify
    rows = await fetch_candidate_rows(pool, batch_run_id, verdict_in, confidence_gte)
    eligible, skipped = classify_eligibility(rows)
    skip_reasons: dict[str, int] = {}
    for s in skipped:
        skip_reasons[s["reason"]] = skip_reasons.get(s["reason"], 0) + 1

    if not confirm:
        first_10 = [
            {
                "lesson_a_id": r["lesson_a_id"], "lesson_b_id": r["lesson_b_id"],
                "a_title": r["a_title"], "b_title": r["b_title"],
                "verdict": r["verdict"], "direction": r["direction"],
                "confidence": float(r["confidence"]),
                "cosine": float(r["cosine_similarity"]),
                "reasoning": r["reasoning"],
            }
            for r in eligible[:10]
        ]
        return json.dumps({
            "preview": True,
            "batch_run_id": batch_run_id,
            "filters": {
                "verdict_in": verdict_in,
                "confidence_gte": confidence_gte,
            },
            "would_apply": min(len(eligible), max_apply),
            "would_skip": len(skipped),
            "skip_reasons": skip_reasons,
            "first_10": first_10,
            "total_eligible": len(eligible),
            "next_step": "call again with confirm=true, reviewer='your-name' to apply",
        })

    # Apply mode
    if not reviewer or not reviewer.strip():
        return json.dumps({"error": "reviewer is required when confirm=true"})

    to_apply = eligible[:max_apply]
    applied_merge_ids: list[int] = []
    apply_errors = 0

    async with pool.acquire() as conn:
        for r in to_apply:
            try:
                verdict_obj = JudgeVerdict(
                    relationship=r["verdict"],
                    direction=r["direction"],
                    confidence=float(r["confidence"]),
                    reasoning=r["reasoning"],
                )
                cosine = float(r["cosine_similarity"])
                model = r["judge_model"]

                if r["verdict"] == "duplicate":
                    canonical_id, merged_id = await _pick_canonical(
                        conn, r["lesson_a_id"], r["lesson_b_id"],
                    )
                    merge_id = await execute_auto_merge(
                        pool,
                        new_lesson_id=merged_id,
                        canonical_id=canonical_id,
                        verdict=verdict_obj,
                        cosine=cosine,
                        judge_model=model,
                        decided_by=reviewer,
                        auto_decided=False,
                    )
                elif r["verdict"] == "supersedes":
                    if r["direction"] == "new→existing":
                        # A supersedes B: retire B, A survives.
                        merge_id = await execute_auto_supersede(
                            pool,
                            new_lesson_id=r["lesson_a_id"],
                            existing_lesson_id=r["lesson_b_id"],
                            verdict=verdict_obj,
                            cosine=cosine,
                            judge_model=model,
                            decided_by=reviewer,
                            auto_decided=False,
                        )
                    elif r["direction"] == "existing→new":
                        # B supersedes A: retire A, B survives.
                        # execute_auto_supersede has a hard guard rejecting
                        # direction='existing→new', so use execute_auto_merge
                        # instead: merge A into B (B is canonical).
                        merge_id = await execute_auto_merge(
                            pool,
                            new_lesson_id=r["lesson_a_id"],
                            canonical_id=r["lesson_b_id"],
                            verdict=verdict_obj,
                            cosine=cosine,
                            judge_model=model,
                            decided_by=reviewer,
                            auto_decided=False,
                        )
                    else:
                        _logger.warning(
                            "supersedes row missing direction: a=%s b=%s",
                            r["lesson_a_id"], r["lesson_b_id"],
                        )
                        apply_errors += 1
                        continue
                else:
                    _logger.warning("unexpected verdict: %s", r["verdict"])
                    apply_errors += 1
                    continue

                applied_merge_ids.append(merge_id)
            except Exception as e:
                _logger.warning(
                    "apply failed for pair (%s,%s): %s",
                    r["lesson_a_id"], r["lesson_b_id"], e,
                )
                apply_errors += 1

    remaining = max(0, len(eligible) - len(to_apply))
    return json.dumps({
        "applied": len(applied_merge_ids),
        "skipped": len(skipped),
        "apply_errors": apply_errors,
        "skip_reasons": skip_reasons,
        "merge_ids": applied_merge_ids,
        "reviewer": reviewer,
        "batch_run_id": batch_run_id,
        "remaining_above_threshold": remaining,
    })
```

- [ ] **Step 2: Register the new tool module in `src/server.py`**

In `src/server.py`, find the tool registration block and add one line after the consolidation import:

```python
import src.tools.consolidation  # noqa: E402, F401
import src.tools.backlog_apply  # noqa: E402, F401   # ← NEW
```

- [ ] **Step 3: Sync and verify everything imports**

```bash
rsync -av ~/dev/claude-memory/src/tools/backlog_apply.py slmbeast:~/dev/claude-memory/src/tools/backlog_apply.py
rsync -av ~/dev/claude-memory/src/server.py slmbeast:~/dev/claude-memory/src/server.py
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && python -c 'import src.server; print(\"server import ok\")'"
```

Expected: `server import ok`

- [ ] **Step 4: Run the full test suite to confirm no regressions**

```bash
ssh slmbeast "cd ~/dev/claude-memory && source venv/bin/activate && pytest tests/ 2>&1 | tail -5"
```

Expected: all previous tests still pass + the 9 new ones (4 canonical + 5 eligibility) = 32 total.

- [ ] **Step 5: Commit**

```bash
git add src/tools/backlog_apply.py src/server.py
git commit -m "feat(v5.2): add apply_backlog_batch MCP tool + register"
```

---

### Task 6: Deploy to Production

**Files:** none

- [ ] **Step 1: Copy files to EC2 + into the MCP container**

```bash
scp -i ~/.ssh/AWS_FR.pem ~/dev/claude-memory/src/tools/backlog_apply.py ubuntu@44.212.169.119:~/claude-memory/src/tools/backlog_apply.py
scp -i ~/.ssh/AWS_FR.pem ~/dev/claude-memory/src/server.py ubuntu@44.212.169.119:~/claude-memory/src/server.py
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker cp ~/claude-memory/src/tools/backlog_apply.py claude_memory_mcp:/app/src/tools/backlog_apply.py && docker cp ~/claude-memory/src/server.py claude_memory_mcp:/app/src/server.py"
```

Expected: file transfers complete, no errors.

- [ ] **Step 2: Rebuild the image and restart the container using the v1 ContainerConfig workaround (lesson #339)**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "cd ~/claude-memory && docker-compose build mcp 2>&1 | tail -5"
```

Expected: `Successfully tagged claude-memory_mcp:latest`, no errors.

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker stop claude_memory_mcp && docker rm claude_memory_mcp && docker run -d --name claude_memory_mcp --network claude-memory_claude_memory_net -p 127.0.0.1:8004:8003 --env-file ~/claude-memory/.env -e DATABASE_URL=\"postgresql://claude:\$(grep POSTGRES_PASSWORD ~/claude-memory/.env | cut -d= -f2)@db:5432/claude_memory\" --restart unless-stopped claude-memory_mcp:latest && sleep 5 && docker ps --format '{{.Names}}: {{.Status}}' | grep claude"
```

Expected: container id echoed; `claude_memory_mcp: Up 5 seconds` in output.

- [ ] **Step 3: Verify container health**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker logs claude_memory_mcp --tail 20 2>&1 | grep -iE 'startup|error|traceback' | head -10"
```

Expected: `Application startup complete.` line; zero errors/tracebacks.

- [ ] **Step 4: No commit — deployment only.**

---

### Task 7: Preview-Mode Smoke Test on Production

**Files:** none

Using the MCP client in this Claude session, call `apply_backlog_batch` in preview mode.

- [ ] **Step 1: Call the tool with default filters**

```
apply_backlog_batch(batch_run_id="pilot-2026-04-21")
```

Expected response fields:
- `preview: true`
- `would_apply: ≈82` (minus any newly-ineligible since 2026-04-20; likely 80-82)
- `would_skip: 0` or very small
- `first_10`: array of 10 pairs, confidence ≥ 0.95 for most

If the tool isn't yet visible in the MCP tool list because the session was started before the container rebuild, the user will need to reload the MCP connection (typically by restarting Claude Desktop or running `/mcp reload` in Claude Code). Note this as a known step.

- [ ] **Step 2: Sanity-check the first_10**

Manually skim the 10 highest-confidence pairs' titles + reasoning. Acceptance: each looks like a genuine duplicate or supersede. If any look wrong, pause and investigate before applying.

- [ ] **Step 3: No commit — smoke test only.**

---

### Task 8: Apply First Batch (max_apply=50)

**Files:** none

- [ ] **Step 1: Call the tool with confirm=true**

```
apply_backlog_batch(
  batch_run_id="pilot-2026-04-21",
  confirm=True,
  reviewer="bjhengen",
  max_apply=50
)
```

Expected response:
- `applied: 50`
- `apply_errors: 0`
- `merge_ids`: array of 50 integers
- `remaining_above_threshold: ≈32`

- [ ] **Step 2: Spot-check 5 random resulting merges**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_db psql -U claude -d claude_memory -c \"
SELECT lm.id, lm.canonical_id, lm.merged_id, lm.action, lm.auto_decided, lm.decided_by,
       lc.title AS canonical_title,
       lm_merged.title AS merged_title,
       lm_merged.retired_at IS NOT NULL AS merged_retired
FROM lesson_merges lm
JOIN lessons lc ON lc.id = lm.canonical_id
JOIN lessons lm_merged ON lm_merged.id = lm.merged_id
WHERE lm.decided_by = 'bjhengen'
ORDER BY lm.id DESC
LIMIT 5;
\""
```

Acceptance: each row shows `auto_decided=f`, `decided_by=bjhengen`, the merged lesson is retired, and the canonical title looks like the better-established lesson.

- [ ] **Step 3: No commit — apply phase.**

---

### Task 9: Apply Remaining Batch

**Files:** none

- [ ] **Step 1: Call the tool again with the same parameters**

```
apply_backlog_batch(
  batch_run_id="pilot-2026-04-21",
  confirm=True,
  reviewer="bjhengen",
  max_apply=50
)
```

Expected response:
- `applied: ≈32`
- `apply_errors: 0`
- `merge_ids`: array of ≈32 integers
- `remaining_above_threshold: 0`

- [ ] **Step 2: Confirm no eligible pairs remain**

```
apply_backlog_batch(batch_run_id="pilot-2026-04-21")
```

Expected: `would_apply: 0`, `would_skip: ≈82` (all skipped as `already_merged` now).

- [ ] **Step 3: Final audit count**

```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@44.212.169.119 "docker exec claude_memory_db psql -U claude -d claude_memory -c \"
SELECT COUNT(*) AS total_applied,
       SUM(CASE WHEN action='merged' THEN 1 ELSE 0 END) AS duplicates,
       SUM(CASE WHEN action='superseded' THEN 1 ELSE 0 END) AS supersedes
FROM lesson_merges
WHERE decided_by = 'bjhengen' AND auto_decided = false;
\""
```

Expected: `total_applied ≈ 82`, ≈68 duplicates + ≈14 supersedes.

- [ ] **Step 4: No commit — apply phase complete.**

---

### Task 10: Merge to Main + Log Findings

**Files:** none (branch merge + memory)

- [ ] **Step 1: Fast-forward merge to main**

```bash
git checkout main
git pull origin main
git merge --ff-only v5-2-backlog-apply
git push origin main
```

Expected: push succeeds.

- [ ] **Step 2: Log a summary lesson via MCP**

Using the MCP `log_lesson` tool, log a lesson summarizing:
- Total pairs applied (≈82)
- Breakdown (duplicates vs supersedes)
- Any surprises (e.g., some pairs skipped as already_merged — that means v5 log-time consolidation already caught them; record how many)
- Corpus size before/after (run `SELECT COUNT(*) FROM lessons WHERE retired_at IS NULL` before merging and after Task 9 to quantify)

Title: `"V5.2 backlog apply — N high-confidence pairs consolidated (2026-04-22)"`.

Tags: `["v5-2", "backlog", "apply", "cleanup"]`.

- [ ] **Step 3: Write a journal entry**

Using `write_journal`, capture qualitative observations: how the apply workflow felt, whether the canonical-selection rule produced sensible survivors, whether you'd want to change the rule before tackling the mid-confidence tier.

- [ ] **Step 4: No code commit — v5.2 is complete.**

---

## Rollback Plan

Each applied pair writes one row to `lesson_merges`. Rollback options, in order of increasing scope:

1. **Individual undo:** use the existing `undo_consolidation(merge_id, reason, reviewer)` MCP tool for any specific pair that looks wrong.
2. **Bulk undo via SQL:** list all v5.2-applied merges:
   ```sql
   SELECT id FROM lesson_merges
   WHERE decided_by = 'bjhengen' AND auto_decided = false
     AND reversed_at IS NULL
     AND created_at >= '2026-04-22';
   ```
   Then call `undo_consolidation` per ID. If this becomes routine, a future `undo_batch` tool is justified, but skip for v5.2.
3. **Kill switch:** there's no v5.2 kill switch — the tool only runs when called, so not calling it is the kill switch.

## What This Plan Does Not Cover

Per the design doc "What v5.2 Does Not Cover" section:

- Mid-confidence tier (194 pairs between 0.60 and the auto-act thresholds).
- Contradicts handling (71 flagged pairs).
- Bulk-undo tool.
- Cross-batch apply.
- Re-judging of changed content.
