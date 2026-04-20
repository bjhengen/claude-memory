# claude-memory v5.1: Backlog Consolidation Analysis

**Date:** 2026-04-20
**Version:** v5.1
**Status:** Design approved; pending implementation plan
**Preceded by:** [v5 Consolidation](./2026-04-20-v5-consolidation-design.md)

## Overview

v5.1 is a **one-shot analytical pass** over the ~700 lessons that predate v5. It runs every above-threshold pair through the v5 judge and records the verdicts in a new `backlog_analysis` table — with zero side-effects on the lessons themselves.

The v5.1 scope is intentionally narrow: **measure, don't act**. No merges, no supersedes, no retirements. The output is a dataset that answers two questions:

1. How much duplication and obsolescence actually exists in the historical corpus?
2. Are v5's autonomy thresholds well-calibrated against this data, or do they need tuning?

Acting on the findings is deferred to a future iteration once the data is reviewed.

## Motivation

V5 evaluates one lesson at a time, at log time. Two gaps remain:

1. **The existing ~700 lessons were never evaluated.** Whatever duplication accumulated over six months of memory use is still there — polluting search rankings, repeating advice, and occasionally contradicting itself.
2. **V5's thresholds (0.90 auto-merge, 0.95 auto-supersede, 0.60 queue-min) were chosen on gut feel.** There's no empirical signal for whether those numbers are right. A judged dataset from the historical corpus gives us the distribution to calibrate against.

A secondary motivation is paper-readiness. The Feb 15 journal entry and later conversations flagged writing an "agent memory" paper with claude-memory as a case study. Any credible paper needs quantitative claims about the corpus and the consolidation mechanism. A backlog analysis produces exactly that.

## Design Decisions

Four decisions were locked via brainstorming before implementation planning:

| # | Decision | Chosen |
|---|---|---|
| 1 | Primary goal | **B then C**: corpus quality measurement first, judge-threshold validation second. Cleanup is a side effect, not the goal. |
| 2 | Action surface | **A — pure dry-run**. New `backlog_analysis` table. Writes go there and nowhere else. No merges, no queue entries, no retirements. |
| 3 | Pair-selection strategy | **B — all pairs above cosine threshold**. Every unique live-live pair with cosine ≥ 0.85 gets judged once. Not per-lesson top-k. Cross-project scoping. |
| 4 | Run location | **A — directly against prod**. New table is additive to the schema. Read-heavy, write-light. Prod traffic unaffected. |

## Architecture

Three additions. No changes to v5 code or tables.

### Component 1: `db/migrations/v5_1_backlog_analysis.sql`

One new table:

```sql
CREATE TABLE IF NOT EXISTS backlog_analysis (
    id SERIAL PRIMARY KEY,
    batch_run_id VARCHAR(100) NOT NULL,
    lesson_a_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    lesson_b_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    cosine_similarity NUMERIC(4,3) NOT NULL,
    judge_model VARCHAR(50) NOT NULL,
    verdict VARCHAR(20) NOT NULL CHECK (verdict IN ('duplicate','supersedes','contradicts','unrelated')),
    direction VARCHAR(30),  -- only set when verdict='supersedes': 'new→existing' or 'existing→new'
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

The `CHECK(lesson_a_id < lesson_b_id)` enforces canonical pair ordering (no `(a,b)` and `(b,a)` duplicates in the dataset). The `UNIQUE(batch_run_id, lesson_a_id, lesson_b_id)` enables resumable runs: a crashed batch can be re-invoked with the same `batch_run_id` and it picks up where it left off without re-judging pairs.

### Component 2: `src/consolidation/backlog.py`

Shared helper module — pulled out of the script so it's unit-testable.

- `generate_pairs(pool, cosine_threshold) -> list[dict]` — executes the pairwise SQL, returns the list of candidate pairs sorted by cosine descending.
- `judge_and_record(pool, anthropic, pair, batch_run_id, judge_model, timeout) -> None` — calls `adjudicate()` (the existing v5 judge), inserts one row. Idempotent on `(batch_run_id, a, b)`.
- `render_report(rows, thresholds) -> tuple[str, dict]` — pure function that takes a list of `backlog_analysis` rows + current v5 thresholds and returns `(markdown_string, json_dict)`.

### Component 3: `scripts/analyze_backlog.py`

CLI entry point.

```
./analyze_backlog.py --batch-id pilot-2026-04-21 [options]

Options:
  --cosine-threshold 0.85    Minimum pairwise cosine (default 0.85)
  --concurrency 10           Max in-flight Anthropic calls (default 10)
  --limit N                  Only process first N candidate pairs (for staged runs)
  --dry-run                  Count candidate pairs and print plan; no Anthropic calls
```

Flow:

1. Connect to DB via `DATABASE_URL`; create `AsyncAnthropic` client via `ANTHROPIC_API_KEY`.
2. Call `generate_pairs(pool, cosine_threshold)` → `N` candidate pairs total.
3. Query existing `backlog_analysis` rows for this `batch_run_id`; filter those out of the work list.
4. If `--dry-run`, print `N total / M already done / K remaining` and exit.
5. Otherwise, for each remaining pair, dispatch `judge_and_record()` under an `asyncio.Semaphore(concurrency)`.
6. Every 25 completed pairs, print a progress line: `[247/1342] verdict=duplicate conf=0.87 cosine=0.91 a=#123 b=#456`.
7. On `KeyboardInterrupt` or `SIGTERM`, wait for in-flight pairs to finish, then exit cleanly.

### Component 4: `scripts/backlog_report.py`

CLI entry point.

```
./backlog_report.py --batch-id pilot-2026-04-21 [options]

Options:
  --format {markdown,json,both}    Output format (default markdown)
  --output PATH                     Write to file instead of stdout
```

Reads `backlog_analysis` rows for the batch and calls `render_report()` with the v5 thresholds pulled from `src.consolidation.config` (same env-var-driven source v5 itself uses — guarantees the report reflects the thresholds actually in effect at report-generation time). No DB writes. The markdown report contains:

- Verdict distribution: count + percent by verdict.
- Confidence histogram: per-verdict, bucketed at 0.1 increments.
- Threshold-crossing counts: given v5's current thresholds (0.90 auto-merge, 0.95 auto-supersede, 0.60 queue-min), how many pairs would have auto-merged / auto-superseded / enqueued / flagged / been ignored.
- Top-20 highest-confidence pairs, with both lesson titles and the reasoning string.

The JSON format is the raw row dump — suitable for loading into a notebook for further analysis.

## Data Flow

**Pair generation (single SQL query):**

```sql
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
```

pgvector computes the cosines server-side; no client loops over N² pairs. Expected result size: ~500–2000 rows for a 700-lesson corpus at threshold 0.85. The `ORDER BY cosine DESC` means if the run is `--limit`ed, the highest-signal pairs get judged first.

**Per-pair flow:**

1. Worker coroutine acquires the semaphore slot.
2. Calls `adjudicate(anthropic, a_title, a_content, b_title, b_content, model, timeout)` — same function v5 uses. Returns a `JudgeVerdict`.
3. Inserts one row into `backlog_analysis` with the verdict, direction, confidence, reasoning, cosine, and `batch_run_id`.
4. Releases the semaphore.

**Error handling:**

- Judge timeout or API error → row gets inserted anyway with `verdict='unrelated'`, `confidence=0.0`, `reasoning='judge error: <type>'`. Same fallback v5 uses — failures are visible in the dataset as zero-confidence rows that can be filtered out for analysis.
- DB insert conflict on the UNIQUE constraint → skipped (pair was already judged in a prior run with the same batch-id). No error raised.
- DB insert failure (other) → logged with pair IDs, pair will be retried on next run thanks to the UNIQUE constraint acting as an idempotency key.
- `KeyboardInterrupt` / `SIGTERM` → asyncio workers finish their in-flight pair, then the main loop exits. Resume by re-invoking with the same `--batch-id`.

## Testing

Minimal unit tests, consistent with v5's convention for thin glue code:

- `tests/test_backlog_pairs.py` — insert 4 fixture lessons with controlled embeddings (two near-duplicates at cosine ≈ 0.95, two unrelated at cosine ≈ 0.3), call `generate_pairs()`, assert only the one expected pair is returned above threshold 0.85 and with correct `a < b` ordering.
- `tests/test_backlog_report.py` — insert 10 fake `backlog_analysis` rows covering all four verdicts across confidence bands, call `render_report()` with the current v5 thresholds, assert the verdict counts and threshold-crossing counts are correct.

No integration test for the full script. Validation is via `--dry-run` on prod, followed by a `--limit 50` staged run, followed by the full run.

## Rollout

Four steps.

1. **Deploy migration to prod.** `scp` the migration file, `docker cp` into the db container, `psql -f`. Empty table, no functional impact.
2. **Dry run on prod.** `./analyze_backlog.py --batch-id pilot-2026-04-21 --dry-run`. Confirms pair count is in the expected range. If it's ~500, proceed; if it's 5000+, tighten the cosine threshold.
3. **Staged run.** `./analyze_backlog.py --batch-id pilot-2026-04-21 --limit 50`. End-to-end flow check: judge calls go through, rows write correctly, progress output looks right. Manually inspect the first few rows.
4. **Full run.** Same command without `--limit`. Resumes from where the staged run left off (step 3's rows stay in the table, UNIQUE constraint skips them). Generate report via `backlog_report.py`.

## Rollback

`DROP TABLE backlog_analysis`. Pure additive schema change — no side effects on lessons, v5 tables, or any other data. Same safety property as v5's migration.

## Cost and Runtime Estimates

- **Pair count:** ~500–2000 unique pairs at cosine ≥ 0.85 for a 700-lesson corpus. This is a projection; the `--dry-run` will give the actual number.
- **Runtime:** at concurrency=10 and ~2s wall time per judge call, ~2–7 minutes wall time for the full run. Negligible compared to the manual review of the report.
- **Anthropic cost:** Haiku 4.5 at ~$0.001 per judge call × ~500–2000 pairs = **$0.50–$2.00 total**.

## What v5.1 Does Not Cover

These are deferred to a later version:

- **An "apply" mechanism.** There is no tool in v5.1 that converts `backlog_analysis` rows into actual `lesson_merges` or `consolidation_queue` entries. The report is reviewed first, thresholds are (possibly) adjusted, and only then does a future iteration add the apply tool.
- **Re-running against updated embeddings.** If the embedding model changes, the cosines change, and we might want to re-judge. Out of scope.
- **Cross-batch comparison tooling.** Diffing two batches to see how judgments shift over time (e.g., if the judge model is upgraded) is interesting but not needed for v5.1.
- **Cross-entity consolidation.** Patterns, specs, agents — same out-of-scope list as v5.

## Success Criteria

v5.1 is successful when all of these are true:

1. The migration applies cleanly to prod with no errors.
2. The full analysis run completes without unhandled exceptions on the ~700-lesson corpus.
3. The generated report shows a verdict distribution and confidence histogram that's usable for threshold calibration.
4. If v5's thresholds were applied to the dataset, the auto-merge count is non-zero and hand-inspection of the top-10 highest-confidence pairs confirms they are genuine duplicates.
5. Judge-error rate (confidence=0.0 rows) is under 5% of total pairs — i.e., the judge is working reliably on real corpus data, not just the two smoke-test lessons.
