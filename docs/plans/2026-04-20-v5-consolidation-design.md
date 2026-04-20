# claude-memory v5: Lesson Consolidation

**Date:** 2026-04-20
**Version:** v5
**Status:** Design approved; pending implementation plan
**Preceded by:** [v4 Feedback Loop](./2026-03-07-v4-feedback-loop-design.md)

## Overview

v5 adds **log-time consolidation** for the lessons table: when a new lesson is logged, the system finds near-duplicates via embedding similarity, asks an LLM judge to classify the relationship (duplicate / supersedes / contradicts / unrelated), and then either auto-acts on high-confidence decisions, queues ambiguous ones for human review, or flags contradictions for resolution.

The core invariant is **no content destruction**: every consolidation action soft-retires the losing lesson and writes a permanent, reversible audit row. Nothing is ever physically deleted.

The v5 scope is intentionally narrow. Backlog processing, cluster/graph summarization, scheduled batch runs, and cross-entity consolidation (patterns, specs) are all out of scope.

## Motivation

As of 2026-04-20 the corpus holds ~700 lessons accumulated over six months. Without consolidation, three failure modes compound over time:

1. **Near-duplicates dilute search ranking.** Two lessons saying the same thing compete for retrieval slots.
2. **Obsolete guidance persists.** When a new lesson supersedes an old one, the old one keeps surfacing and may mislead future agents.
3. **Contradictions go undetected.** Lesson #228 and the Resonance project's experience (lessons #186, #249, #257) established that contradiction-triggered decay is essential for memory quality — but no mechanism exists to surface the contradictions.

The design decision from lesson #228 (*"Memory decay should be contradiction-triggered, not time-based"*) is load-bearing: v5 is a contradiction-and-duplication detector, not a time-based expiration system.

## Design Decisions

Nine decisions were locked via brainstorming before implementation planning:

| # | Decision | Chosen |
|---|---|---|
| 1 | Scope | **A+B+D**: merge near-duplicates, supersede obsolete lessons, flag contradictions. Cluster/summarize (C) deferred. |
| 2 | Safety posture | **Tiered autonomy**: high-confidence auto-acts; medium-confidence enqueues; contradictions always flag. |
| 3 | Judge architecture | **Embedding-gated LLM adjudication** (pairwise). Cluster/graph pass deferred. |
| 4 | Trigger mechanism | **Log-time only**. Backlog is out of scope for v5. |
| 5 | Merge semantics | **Preserve-and-link**. Losing lesson soft-retires with pointer to canonical; no content destruction. |
| 6 | Autonomy thresholds | **Per-action asymmetric**: duplicate ≥ 0.90 auto, supersede ≥ 0.95 auto, contradiction always flagged. |
| 7 | Rating/annotation carryover | **Transfer with audit-row provenance**. Counters added to canonical; annotations repointed; provenance preserved in `lesson_merges`. |
| 8 | Measurement | **D-staged**: reversal-tracking schema ships with v5; held-out labeled eval set built as pre-paper task. |
| 9 | Review queue UX | **Dedicated MCP tools + auto-annotations on affected lessons** (both). |

LLM judge default: **Haiku** (`claude-haiku-4-5-20251001`), swappable to Sonnet via config flag for paper evaluation.

## Architecture

Consolidation is a log-time interceptor inside `log_lesson`. Three components:

1. **Candidate finder** (`consolidation/candidates.py`) — pgvector ANN query for top-k lessons with cosine ≥ 0.85. Pure SQL. ~50ms.
2. **Judge** (`consolidation/judge.py`) — Haiku call with structured output. Per-pair verdict. ~300–500ms (parallel across candidates).
3. **Actor** (`consolidation/actor.py`) — routes verdicts to auto-action, queue, flag, or passthrough. All DB writes.

**Integration point:** a single new function `consolidate_at_log(new_lesson, app)` called from `log_lesson` after embedding generation and before the normal INSERT. Failures inside consolidation never break `log_lesson` — exception → log → fall through to plain insert.

**Kill switch:** `CONSOLIDATION_ENABLED=false` short-circuits consolidation entirely.

## Data Model

Three new tables. Migration file: `db/migrations/v5_consolidation.sql`. No changes to `lessons`, `ratings` (counters already on `lessons`), or `annotations`.

### `lesson_merges`

Audit trail for duplicate-merge and supersede actions. Every action — auto or human-approved — writes one row. Reversal is a column update, not a delete.

```sql
CREATE TABLE lesson_merges (
    id SERIAL PRIMARY KEY,
    canonical_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    merged_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    action VARCHAR(20) NOT NULL CHECK (action IN ('merged', 'superseded')),
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    auto_decided BOOLEAN NOT NULL,
    decided_by VARCHAR(100),  -- NULL when auto_decided=true
    transferred_upvotes INT NOT NULL DEFAULT 0,
    transferred_downvotes INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    reversed_at TIMESTAMP,
    reversed_by VARCHAR(100),
    reversed_reason TEXT,
    UNIQUE(canonical_id, merged_id),
    CHECK (auto_decided = true OR decided_by IS NOT NULL),
    CHECK (canonical_id <> merged_id)
);

CREATE INDEX idx_lesson_merges_canonical ON lesson_merges(canonical_id) WHERE reversed_at IS NULL;
CREATE INDEX idx_lesson_merges_merged ON lesson_merges(merged_id) WHERE reversed_at IS NULL;
```

### `lesson_conflicts`

Flagged contradictions awaiting human resolution. Read-only until resolved.

```sql
CREATE TABLE lesson_conflicts (
    id SERIAL PRIMARY KEY,
    lesson_a_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    lesson_b_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    flagged_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,
    resolved_by VARCHAR(100),
    resolution VARCHAR(20) CHECK (resolution IN ('kept_a', 'kept_b', 'kept_both', 'irrelevant')),
    resolution_note TEXT,
    CHECK (lesson_a_id < lesson_b_id),  -- canonical ordering prevents (A,B)+(B,A) dupes
    UNIQUE(lesson_a_id, lesson_b_id)
);

CREATE INDEX idx_lesson_conflicts_unresolved ON lesson_conflicts(flagged_at) WHERE resolved_at IS NULL;
```

### `consolidation_queue`

Medium-confidence proposals awaiting human approve/reject.

```sql
CREATE TABLE consolidation_queue (
    id SERIAL PRIMARY KEY,
    new_lesson_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    candidate_lesson_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    proposed_action VARCHAR(20) NOT NULL CHECK (proposed_action IN ('merged', 'superseded')),
    proposed_direction VARCHAR(20),  -- 'new→existing' or 'existing→new'
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    enqueued_at TIMESTAMP DEFAULT NOW(),
    decided_at TIMESTAMP,
    decided_by VARCHAR(100),
    decision VARCHAR(20) CHECK (decision IN ('approved', 'rejected')),
    decision_note TEXT
);

CREATE INDEX idx_consolidation_queue_pending ON consolidation_queue(enqueued_at) WHERE decided_at IS NULL;
```

### Judge output schema

Enforced via Anthropic structured-output (Pydantic model):

```python
class JudgeVerdict(BaseModel):
    relationship: Literal["duplicate", "supersedes", "contradicts", "unrelated"]
    direction: Literal["new→existing", "existing→new"] | None  # required for "supersedes"
    confidence: float  # 0.0–1.0
    reasoning: str  # ≤200 tokens
```

### Thresholds as env vars

```
CONSOLIDATION_ENABLED=true
CONSOLIDATION_CANDIDATE_COSINE=0.85
CONSOLIDATION_AUTO_MERGE_CONFIDENCE=0.90
CONSOLIDATION_AUTO_SUPERSEDE_CONFIDENCE=0.95
CONSOLIDATION_QUEUE_MIN_CONFIDENCE=0.60
CONSOLIDATION_JUDGE_MODEL=claude-haiku-4-5-20251001
CONSOLIDATION_JUDGE_TIMEOUT_SECONDS=2
CONSOLIDATION_CANDIDATE_TOP_K=5
```

### Canonical-choice rule

- **Duplicate action**: existing lesson is always canonical at log time. New lesson is retired with `retired_reason="merged into N"`.
- **Supersede action**: judge's `direction` field is authoritative. `new→existing` → new is canonical, existing is retired. `existing→new` → new is retired (existing already covers new's claim).

### Tags and severity on merge

- **Tags**: union'd into canonical (`UPDATE lessons SET tags = ARRAY(SELECT DISTINCT unnest(tags || $1))`). Keyword-level; no meaning change.
- **Severity**: canonical takes max (critical > important > tip). Prevents losing urgency.

### Cross-project scoping

Candidate finder filters candidates to **same project_id or NULL on either side**. Cross-project merges are not considered at log time (domain context differs).

## Control Flow at Log Time

Flow inside `log_lesson` after step 1 (title-dedup) and step 2 (embedding generation):

### Step 3 — Candidate finder

Query top-5 lessons by cosine similarity where `cosine ≥ CONSOLIDATION_CANDIDATE_COSINE` (0.85), scoped to same project or NULL project, excluding retired lessons. If none, skip to step 7.

### Step 4 — Judge (parallel)

Call Haiku once per candidate with structured output. Collect all verdicts. ~300–500ms wall-clock (parallel). Skip `relationship="unrelated"`.

### Step 5 — Pick best verdict

Highest-confidence non-unrelated verdict wins. Ties broken by higher cosine.

### Step 6 — Route by (relationship, confidence)

| Verdict | Confidence | Action |
|---|---|---|
| `unrelated` | any | Step 7 (normal insert) |
| `duplicate` | ≥ 0.90 | INSERT new → retire new, audit row, transfer rating counters (from new to canonical; 0/0 at log time), repoint annotations, auto-annotate canonical |
| `duplicate` | 0.60–0.89 | INSERT new → create `consolidation_queue` row, auto-annotate both lessons |
| `supersedes`, dir=`new→existing` | ≥ 0.95 | INSERT new → retire existing, audit row (`action='superseded'`), transfer existing's counters to new, repoint annotations |
| `supersedes`, dir=`existing→new` | ≥ 0.95 | INSERT new → retire new (existing already covers the claim) |
| `supersedes` | 0.60–0.94 | INSERT new → create `consolidation_queue` row, auto-annotate both |
| `contradicts` | ≥ 0.60 | INSERT new normally → create `lesson_conflicts` row, auto-annotate both |
| any | < 0.60 | Step 7 (normal insert) |

### Step 7 — Normal insert

Unchanged from current `log_lesson` behavior.

### Latency budget

+~500ms p50 added to `log_lesson`. p99 capped by judge timeout (2s). Acceptable — `log_lesson` is not a hot path.

### Observability

Each consolidation action emits a structured log line: `candidate_count, best_verdict, confidence, action_taken, elapsed_ms`.

## MCP Tools

Six new tools in `src/tools/consolidation.py`. Running total: **48 → 54**.

### Queue management

```python
list_pending_consolidations(project: str = None, limit: int = 20) -> json
# Returns list of queue entries with new/candidate titles, proposed action,
# confidence, reasoning, age_days.

approve_consolidation(queue_id: int, reviewer: str = None) -> json
# Executes the merge/supersede per the proposal.
# Writes lesson_merges row with auto_decided=false, decided_by=reviewer.
# Verifies canonical is not retired before acting; returns error if so.
# Marks queue row decided. Clears pending annotations.

reject_consolidation(queue_id: int, reason: str = None, reviewer: str = None) -> json
# Marks queue row decided='rejected'. Clears pending annotations.
# Does NOT modify lessons.
```

### Conflict resolution

```python
list_conflicts(project: str = None, unresolved_only: bool = True, limit: int = 20) -> json

resolve_conflict(conflict_id: int,
                 resolution: str,  # 'kept_a' | 'kept_b' | 'kept_both' | 'irrelevant'
                 note: str = None,
                 reviewer: str = None) -> json
# 'kept_a' → retires lesson B (retired_reason="conflict resolved: A preferred")
# 'kept_b' → retires lesson A (symmetric)
# 'kept_both' → no lesson change; mark conflict resolved
# 'irrelevant' → false positive; mark resolved, no change
# Clears conflict annotations on both lessons.
```

### Undo

```python
undo_consolidation(merge_id: int, reason: str, reviewer: str = None) -> json
# Sets lesson_merges.reversed_at + reversed_by + reversed_reason.
# Un-retires the merged/superseded lesson.
# Subtracts transferred_upvotes/downvotes from canonical.
# KNOWN LIMITATION: does NOT move annotations back.
```

### Auto-annotation templates

Free-form text on `annotations` table (v4 schema, no change needed). Inserted/cleared by the Actor. Surface via v4 annotation auto-injection.

| Event | Attached to | Template |
|---|---|---|
| Enqueued | Both lessons | `⏸ Consolidation pending (queue #{Q}): proposed {action} with lesson #{other}. Confidence {c}. Run approve_consolidation({Q}) or reject_consolidation({Q}).` |
| Queue approved | Canonical | Replaced with standard merge/supersede annotation (below). Pending annotations cleared. |
| Queue rejected | Both | Pending annotations cleared. No permanent annotation added. |
| Auto-merge / approved | Canonical | `📎 Merged from lesson #{M} on {date}: {judge_reasoning}` |
| Auto-supersede / approved | Canonical | `↗ Supersedes lesson #{M} on {date}: {judge_reasoning}` |
| Conflict flagged | Both lessons | `⚠ Conflicts with lesson #{other} (confidence {c}): {reasoning}. Resolve via resolve_conflict({C}).` |
| Conflict resolved | Both | Conflict annotations cleared. |
| Undo | Canonical | `↺ Merge #{M} reversed by {reviewer}: {reason}` |

## Error Handling & Edge Cases

**Core invariant:** consolidation failures cannot break `log_lesson`.

### Exception wrapping

`consolidate_at_log` sits inside a try/except in `log_lesson`. Any exception → structured error log + metrics counter + fall through to normal insert.

### Transactional boundaries

Each consolidation action is one DB transaction covering: lesson INSERT, retire UPDATE (if applicable), audit/queue/conflict INSERT, and annotation INSERT. Failure → rollback → fall through to normal insert.

### Specific edge cases

| Case | Handling |
|---|---|
| Malformed judge JSON | Structured-output schema prevents it. Fallback: treat as `unrelated`. |
| Judge timeout | Treat as `unrelated`. Log timeout. |
| OpenAI embedding fails | Inherits existing `log_lesson` fatal behavior. Consolidation no-ops. |
| Candidate retired since embedding snapshot | Candidate query filters `retired_at IS NULL`. |
| Concurrent `log_lesson` of near-dupes | Both insert. Accepted limitation. |
| Approve queue entry with retired canonical | `approve_consolidation` returns error suggesting reject. |
| Undo of merge whose canonical was later superseded | Un-retire succeeds; revived lesson is semantically obsolete. Documented. |
| Null/empty judge reasoning | Store `"(no reasoning provided)"`. |
| Burst of 100 logs | ~500 Haiku calls, ~$0.05. No rate limit in v5. Documented. |
| `CONSOLIDATION_ENABLED=false` mid-request | Takes effect per-request. Safe. |
| `resolve_conflict` with no undo | One-way by design. Manual correction via SQL if wrong. |

### Accepted limitations

1. **Systematic judge bias.** Mitigation: held-out eval set + reversal-rate monitoring. If drift detected, flip judge to Sonnet.
2. **Quiet concurrent-dup window** (~500ms). Mitigation: none in v5. Future backlog tool catches.
3. **Annotations don't reverse on undo.** Documented limitation. Requires schema change deferred to future.

## Testing

### Unit tests (`tests/consolidation/`)

- **candidate_finder**: threshold filtering, project scoping, retired-filtering, top-k bounds
- **judge**: mocked Anthropic returning each verdict type; timeout path; malformed-JSON fallback
- **actor**: each routing path (auto-merge, auto-supersede, enqueue, flag-conflict, passthrough); tag-union; severity escalation; rating-counter transfer; annotation repointing; transaction rollback on mid-flight failure

### Integration tests (`tests/integration/test_log_lesson_consolidation.py`)

Real Postgres (testcontainers), stubbed OpenAI embedding client, mocked Anthropic judge.

- Log lesson with no close candidates → plain insert
- Log duplicate → auto-merge
- Log ambiguous match → enqueue + annotations
- Log contradiction → insert with flag + annotations
- `CONSOLIDATION_ENABLED=false` → plain insert, no side effects
- Judge exception → plain insert, error logged
- Judge timeout → plain insert, timeout logged
- Concurrent insert race → both insert (documents accepted limitation)

### Tool tests (`tests/tools/test_consolidation.py`)

Each of the 6 MCP tools. Approve-on-retired-canonical error. Undo flow (un-retire + counter rollback). `list_pending_consolidations` project scoping.

### Success criteria for v5 shipment

- All unit + integration + tool tests pass
- Manual smoke: log 5 hand-crafted duplicate pairs on staging → auto-merge + audit + annotations verified
- Manual smoke: log 3 contradictions → flag + both annotations verified
- `undo_consolidation` fully reverses state
- Kill switch verified

## Evaluation (paper-facing, separate milestone)

Staged as a pre-paper task, not a v5 blocker.

### Held-out labeled set (`evaluation/consolidation/`)

1. **Sample** ~200 candidate pairs from existing 700-lesson corpus at cosine ≥ 0.75 (broader than production). Stratify by project.
2. **Label** each pair as `duplicate / supersedes(direction) / contradicts / unrelated`. ~2–3 hours focused work. Committed to `labels.jsonl`.
3. **Eval script** (`run_eval.py`): runs judge against labeled set. Reports precision/recall/F1 per verdict, plus per-confidence-bucket calibration.
4. **Run twice**: Haiku and Sonnet. Paper's ablation data.
5. **Output**: CSV + markdown summary.

### Production monitoring

Structured log lines + `scripts/consolidation_metrics.sql` aggregating auto-action count, reversal rate, queue depth, conflict-resolution rate. No dashboard required for v5.

### Paper-readiness criteria

- ≥ 100 labeled pairs
- Haiku + Sonnet both evaluated; per-verdict F1 reported
- ≥ 30 days production data, ≥ 50 auto-actions, measurable reversal rate
- Literature-survey comparison table vs Letta / Mem0 / A-MEM / context-hub / Zep / Cognee

## Out of Scope (v5)

Explicitly deferred to keep v5 shippable:

1. **Backlog processing.** Existing 700 lessons untouched. Future on-demand tool or SQL script.
2. **Cluster/graph summarization** (original scope option C). Higher novelty but higher slop risk. Revisit post-paper.
3. **Scheduled batch runs.** Only makes sense once log-time consolidation is trusted.
4. **Cross-entity consolidation** (patterns, specs). Lessons only for v5.
5. **Cross-project consolidation.** Scoped out in candidate finder. Reconsider if domain drift becomes evident.
6. **Automatic memory formation** (the "agents rarely call `log_lesson`" gap). Separate concern from consolidation.
7. **Annotation reversal on undo.** Schema-deferred.
8. **`re_flag_conflict` for changed-mind resolutions.** Manual SQL correction for v5.

## Open Questions (non-blocking)

1. Should `undo_consolidation` emit a journal entry automatically? Leaning no — annotations + audit row already capture it.
2. Should queue entries expire? Not in v5. Documented as accepted behavior.

## Paper Positioning

v5 is also a forcing function for the paper (see separate discussion). Two aspects of the design are deliberate paper hooks:

1. **Per-action asymmetric thresholds** (decision #6) gives a clean ablation: "uniform vs asymmetric at equal false-positive rate."
2. **Log-time (not post-hoc) consolidation** (decision #4) is a distinct positioning vs the comparable systems (Mem0, A-MEM, context-hub), which treat consolidation as batch work.

Neither is sufficient for a paper on its own. The paper requires the held-out labeled set, ≥30 days of production data, and the literature-survey comparison table — all scoped as pre-paper work above, not as v5 blockers.

## References

- Lesson #228 — Memory decay should be contradiction-triggered, not time-based
- Lessons #186, #249, #257 — Resonance project experience with consolidation failures
- [v4 Feedback Loop design](./2026-03-07-v4-feedback-loop-design.md) — rating counters, annotations, hybrid search
- PersonaVLM (arXiv 2604.13074) — persona persistence for multimodal LLMs (adjacent, not a direct comparable)
- Andrew Ng, context-hub — source of v4 rating-and-annotation patterns; direct comparable for v5 paper positioning
