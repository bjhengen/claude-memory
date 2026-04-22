# claude-memory v5.2: Backlog Batch Apply

**Date:** 2026-04-22
**Version:** v5.2
**Status:** Design approved; pending implementation plan
**Preceded by:** [v5.1 Backlog Analysis](./2026-04-20-v5-1-backlog-analysis-design.md)

## Overview

v5.2 adds **one MCP tool** — `apply_backlog_batch` — that converts high-confidence rows from the `backlog_analysis` table into actual `lesson_merges` entries via v5's existing merge/supersede helpers. The tool has a preview mode (default) and an apply mode (requires `confirm=true`), with a per-call cap to prevent accidental bulk apply.

The v5.2 scope is intentionally narrow: **only the 82 high-confidence pairs identified by v5.1** (68 duplicates ≥0.90 + 14 supersedes ≥0.95). The 194 mid-confidence pairs and 71 flagged contradictions are out of scope.

## Motivation

V5.1 ran the backlog analysis and produced a dataset proving two things:

1. The corpus is 46.3% redundant above cosine 0.85 (dup + supersede).
2. 82 pairs are at or above v5's own auto-act confidence thresholds — these are the pairs v5 would have auto-applied if it had been running during their creation.

Applying these 82 is:
- **Low-risk** — they're above the same thresholds v5 uses in production at log time, so this is no more autonomous than v5 already is.
- **High-value** — retires ~82 redundant lessons, cleans search ranking, reduces future-agent noise.
- **Mechanism validation** — proves the apply-from-analysis path works before v5.3 tackles the harder mid-confidence tier.

The audit trail stays clean because the existing v5 `execute_auto_merge` / `execute_auto_supersede` helpers already support `auto_decided=False` + a `decided_by` reviewer string. v5.2 applies pairs as human-reviewed (not auto-decided), even though the original judgments were produced by Haiku.

## Design Decisions

Four decisions were locked via brainstorming before implementation planning:

| # | Decision | Chosen |
|---|---|---|
| 1 | Scope | **A — high-confidence only (82 pairs)**. The 194 mid-confidence and 71 contradicts are deferred. |
| 2 | Apply mechanism | **A — new MCP tool** `apply_backlog_batch`. Interactive workflow via Claude session; no CLI script. |
| 3 | Safety rails | **C — minimal + per-batch cap + mandatory `reviewer`**. Confirm flag, max_apply default 50, required reviewer string. |
| 4 | Idempotency | **A — strict skip** of stale pairs. Already-merged or already-retired lessons get skipped with reason codes, never acted on. |

## Architecture

One new file. No schema changes. No changes to v5 tables.

### Component: `src/tools/backlog_apply.py`

Registers one MCP tool:

```
apply_backlog_batch(
    batch_run_id: str,
    confirm: bool = False,
    verdict_in: list[str] = ["duplicate", "supersedes"],
    confidence_gte: float = 0.90,
    max_apply: int = 50,
    reviewer: str = None,
) -> str  # JSON
```

And one helper (private): `_pick_canonical(conn, a_id, b_id) -> (canonical_id, merged_id)` for the duplicate case, where neither side is "new."

**`_pick_canonical` rule:**
1. Higher upvotes wins.
2. Ties broken by older `created_at` (more established lesson).
3. Final tiebreak: lower id (deterministic).

### Registered in `src/server.py`

One-line addition in the tool registration block, matching the v5 consolidation module pattern:

```python
import src.tools.backlog_apply  # noqa: E402, F401
```

## Tool Behavior

### Preview mode — `confirm=False` (default)

Flow:

1. Query `backlog_analysis` for rows matching `batch_run_id`, `verdict_in`, and `confidence >= confidence_gte`; ordered by `confidence DESC`.
2. For each candidate, check eligibility:
   - Both lessons still live (neither has `retired_at` set)
   - Neither lesson appears as `canonical_id` or `merged_id` in `lesson_merges` for a non-reversed merge
3. Return a JSON summary — **no DB writes**:

```json
{
  "preview": true,
  "batch_run_id": "...",
  "filters": {"verdict_in": [...], "confidence_gte": 0.90},
  "would_apply": N,
  "would_skip": M,
  "skip_reasons": {"already_retired": X, "already_merged": Y},
  "first_10": [
    {
      "lesson_a_id": ..., "lesson_b_id": ...,
      "a_title": "...", "b_title": "...",
      "verdict": "duplicate", "confidence": 0.95,
      "reasoning": "..."
    },
    ...
  ],
  "next_step": "call again with confirm=true, reviewer='your-name' to apply first max_apply"
}
```

### Apply mode — `confirm=True`

Flow:

1. Validate `reviewer` is non-empty. Return error if missing.
2. Same query + eligibility filter as preview.
3. Slice to the first `max_apply` eligible pairs.
4. For each pair, dispatch the correct v5 helper:

| Backlog row verdict | Direction | Action |
|---|---|---|
| `duplicate` | (null) | `_pick_canonical(a, b)` → `execute_auto_merge(new=merged, canonical, ..., auto_decided=False, decided_by=reviewer)` |
| `supersedes` | `new→existing` | `execute_auto_supersede(new=a, existing=b, ..., auto_decided=False, decided_by=reviewer)` |
| `supersedes` | `existing→new` | `execute_auto_supersede(new=b, existing=a, ..., auto_decided=False, decided_by=reviewer)` |

Each helper call writes one `lesson_merges` row with `auto_decided=false` and `decided_by=reviewer`.

5. Individual failures get caught, logged, counted in `apply_errors`; don't abort the whole batch.

6. Return a JSON summary:

```json
{
  "applied": N,
  "skipped": M,
  "apply_errors": K,
  "skip_reasons": {"already_retired": X, "already_merged": Y},
  "merge_ids": [101, 102, ...],
  "reviewer": "bjhengen",
  "batch_run_id": "pilot-2026-04-21",
  "remaining_above_threshold": R
}
```

Where `remaining_above_threshold` is the count of *still-eligible* pairs not processed due to `max_apply` — tells you whether to call again.

## Data Flow

**Eligibility query (used in both preview and apply):**

```sql
WITH merged_lesson_ids AS (
  SELECT canonical_id AS lesson_id FROM lesson_merges WHERE reversed_at IS NULL
  UNION
  SELECT merged_id AS lesson_id FROM lesson_merges WHERE reversed_at IS NULL
)
SELECT
  ba.lesson_a_id, ba.lesson_b_id, ba.verdict, ba.direction,
  ba.confidence, ba.cosine_similarity, ba.reasoning,
  la.title AS a_title, la.retired_at IS NOT NULL AS a_retired,
  lb.title AS b_title, lb.retired_at IS NOT NULL AS b_retired,
  la.id IN (SELECT lesson_id FROM merged_lesson_ids) AS a_in_merges,
  lb.id IN (SELECT lesson_id FROM merged_lesson_ids) AS b_in_merges,
  ba.judge_model
FROM backlog_analysis ba
JOIN lessons la ON la.id = ba.lesson_a_id
JOIN lessons lb ON lb.id = ba.lesson_b_id
WHERE ba.batch_run_id = $1
  AND ba.verdict = ANY($2)
  AND ba.confidence >= $3
ORDER BY ba.confidence DESC
```

Python-side classification:
- `a_retired OR b_retired` → skip with reason `already_retired`
- `a_in_merges OR b_in_merges` → skip with reason `already_merged`
- Otherwise → eligible

## Testing

Two unit tests, consistent with v5 / v5.1 conventions:

- **`tests/test_apply_canonical.py`** — pure-ish function tests for `_pick_canonical`:
  - Two lessons, A has more upvotes → A is canonical
  - Equal upvotes, A is older → A is canonical
  - Equal upvotes + equal timestamps → lower id is canonical (deterministic)
- **`tests/test_apply_eligibility.py`** — integration test with DB fixtures:
  - 4 `backlog_analysis` rows (2 live+eligible, 1 with a retired lesson, 1 with an already-merged lesson)
  - Call the eligibility filter
  - Assert skip reasons match expected

No test for the MCP tool registration or the full tool call path — same convention as v5/v5.1. Exercised by manual smoke on prod.

## Rollout

Five steps:

1. **Implement + test on slmbeast** — same rsync-to-slmbeast / pytest-via-SSH loop as v5/v5.1. Apply migration-free (no DB changes this iteration).
2. **Deploy to prod** — scp new `src/tools/backlog_apply.py`, scp updated `src/server.py` with the tool-registration line, `docker cp` into `claude_memory_mcp`, stop/rm/run the container using the docker-compose v1 ContainerConfig workaround (lesson #339).
3. **Verify tool registration** — confirm `apply_backlog_batch` appears in the MCP tool list.
4. **Preview-mode test on prod** — call `apply_backlog_batch(batch_run_id="pilot-2026-04-21")`. Expected: `would_apply` ≈ 82 (minus any that became ineligible since 2026-04-20), `first_10` shows the highest-confidence duplicates and supersedes. No DB writes.
5. **Apply in two batches** — call with `confirm=true, reviewer="bjhengen", max_apply=50`; inspect response + spot-check a few resulting `lesson_merges` rows. Second call with `max_apply=50` clears the remaining ~32.

## Rollback

Each applied pair produces one `lesson_merges` row. The existing v5 `undo_consolidation(merge_id, reason, reviewer)` tool reverses individual merges.

For bulk undo, run a SQL query to list the relevant merge IDs:

```sql
SELECT id FROM lesson_merges
WHERE decided_by = 'bjhengen'
  AND auto_decided = false
  AND created_at > '2026-04-22 ...'
  AND reversed_at IS NULL;
```

Then call `undo_consolidation` per ID. If bulk undo becomes a recurring need, a v5.3 `undo_batch` tool is justifiable — but premature now (YAGNI).

## What v5.2 Does Not Cover

Deferred to later iterations:

- **Mid-confidence tier.** The 194 pairs between 0.60 and the auto-act thresholds need a different UX — likely batch review with rejection and maybe Sonnet re-judgment. Out of scope.
- **Contradicts handling.** The 71 flagged pairs require human judgment per pair. Not a batch operation.
- **Bulk-undo tool.** Add only if the per-pair `undo_consolidation` proves insufficient in practice.
- **Cross-batch apply.** Works on one `batch_run_id` at a time.
- **Re-judging.** If content changed since analysis (uncommon but possible), we skip — we don't re-judge. A future iteration could add `re_judge=True` if needed.

## Success Criteria

v5.2 is successful when all of these are true:

1. `apply_backlog_batch` tool registers without errors and appears in the MCP tool list.
2. Preview mode returns accurate `would_apply` / `would_skip` counts against the `pilot-2026-04-21` batch.
3. Apply mode with `reviewer="bjhengen"` and `max_apply=50` creates 50 `lesson_merges` rows, all with `auto_decided=false` and `decided_by="bjhengen"`.
4. Second apply call processes the remaining ~32 pairs.
5. Spot-check of 5 random resulting merges confirms the canonical/merged direction is sensible (the surviving lesson is the more established / higher-rated one).
6. Zero apply_errors in either batch.
