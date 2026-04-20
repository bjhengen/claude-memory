# V5 Consolidation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add log-time lesson consolidation (merge near-duplicates, supersede obsolete, flag contradictions) to claude-memory with preserve-and-link semantics, per-action asymmetric thresholds, and an audit trail that supports reversal.

**Architecture:** A new `src/consolidation/` package with three internal modules (`candidates.py`, `judge.py`, `actor.py`) plus one `consolidate_at_log` orchestrator. `src/tools/consolidation.py` adds six new MCP tools for queue management, conflict resolution, and undo. Integration point: one call added to `log_lesson` after embedding generation. Three new DB tables (`lesson_merges`, `lesson_conflicts`, `consolidation_queue`) in a single migration file. Anthropic Haiku used as the judge via JSON-in-prompt + parse pattern (no new SDK feature dependencies).

**Tech Stack:** PostgreSQL 16 (pgvector, asyncpg), Python 3.11, FastMCP, Anthropic SDK (new dep), pytest + pytest-asyncio (new dev deps), OpenAI (existing — embeddings only).

**Design doc:** `docs/plans/2026-04-20-v5-consolidation-design.md` (c530822)

**Testing approach note:** The codebase currently has no test infrastructure. This plan introduces a minimal pytest scaffolding (Tasks 1–2) and writes unit tests for the components where logic density is highest: candidate finder (scoping/filtering correctness), judge (parse/timeout robustness), actor routing (pure-function threshold decisions), and actor DB paths (state transitions). Full integration tests via testcontainers are deferred (following the v4 convention). The six MCP tool endpoints are validated by manual smoke tests in Task 16, not pytest — they are thin wrappers over the already-tested actor functions, and their correctness is more productively verified against a running server.

---

### Task 1: Add Dependencies

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`

**Step 1: Add Anthropic SDK to `requirements.txt`**

Append to `requirements.txt`:

```
# Anthropic SDK for consolidation judge (v5)
anthropic>=0.40.0
```

**Step 2: Create `requirements-dev.txt` for unit-test deps**

```
-r requirements.txt

# Testing
pytest>=8.0.0
pytest-asyncio>=0.23.0
pytest-mock>=3.12.0
```

**Step 3: Install locally**

Run: `pip install -r requirements-dev.txt`
Expected: Anthropic + pytest installed without version conflicts.

**Step 4: Commit**

```bash
git add requirements.txt requirements-dev.txt
git commit -m "chore(v5): add anthropic SDK and pytest dev deps"
```

---

### Task 2: Test Scaffolding

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`
- Create: `pytest.ini`

**Step 1: Create `pytest.ini` at the repo root**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
```

**Step 2: Create `tests/__init__.py`**

Empty file:

```python
```

**Step 3: Create `tests/conftest.py` with DB and mock fixtures**

```python
"""Shared pytest fixtures for claude-memory tests."""

import os
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest


@pytest.fixture
async def db_pool():
    """
    Connection pool for a local test database.

    Assumes a local Postgres with a database named 'claude_memory_test'
    pre-created by the developer. Each test runs its own transactions;
    no cross-test isolation is provided beyond what the tests themselves enforce.
    """
    url = os.getenv("TEST_DATABASE_URL", "postgresql://claude:claude@localhost:5432/claude_memory_test")
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest.fixture
def mock_openai():
    """Mock OpenAI client that returns a fixed embedding."""
    client = MagicMock()
    client.embeddings = MagicMock()
    client.embeddings.create = AsyncMock()
    client.embeddings.create.return_value = MagicMock(
        data=[MagicMock(embedding=[0.1] * 1536)]
    )
    return client


@pytest.fixture
def mock_anthropic():
    """Mock Anthropic client for judge testing."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client
```

**Step 4: Create the test database locally**

Run: `createdb -U claude claude_memory_test 2>/dev/null || echo "already exists"`
Expected: silent success OR "already exists".

**Step 5: Verify pytest discovers an empty test dir**

Run: `pytest --collect-only`
Expected: `no tests ran` (exit 5 is OK here).

**Step 6: Commit**

```bash
git add tests/__init__.py tests/conftest.py pytest.ini
git commit -m "chore(v5): add pytest scaffolding with db_pool and mock fixtures"
```

---

### Task 3: SQL Migration File — Three Tables

**Files:**
- Create: `db/migrations/v5_consolidation.sql`

**Step 1: Write the migration file**

```sql
-- =============================================================================
-- Migration: v5_consolidation.sql
-- Date: 2026-04-20
-- Purpose: Add log-time lesson consolidation infrastructure
--   1. lesson_merges - audit trail for merge/supersede (reversible)
--   2. lesson_conflicts - flagged contradictions awaiting resolution
--   3. consolidation_queue - medium-confidence proposals awaiting human decision
-- Idempotent: safe to run multiple times
-- =============================================================================

-- =============================================================================
-- 1. lesson_merges: audit trail for duplicate-merge and supersede actions
-- =============================================================================

CREATE TABLE IF NOT EXISTS lesson_merges (
    id SERIAL PRIMARY KEY,
    canonical_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    merged_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    action VARCHAR(20) NOT NULL CHECK (action IN ('merged', 'superseded')),
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    auto_decided BOOLEAN NOT NULL,
    decided_by VARCHAR(100),
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

CREATE INDEX IF NOT EXISTS idx_lesson_merges_canonical
    ON lesson_merges(canonical_id) WHERE reversed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_lesson_merges_merged
    ON lesson_merges(merged_id) WHERE reversed_at IS NULL;

-- =============================================================================
-- 2. lesson_conflicts: flagged contradictions
-- =============================================================================

CREATE TABLE IF NOT EXISTS lesson_conflicts (
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
    CHECK (lesson_a_id < lesson_b_id),
    UNIQUE(lesson_a_id, lesson_b_id)
);

CREATE INDEX IF NOT EXISTS idx_lesson_conflicts_unresolved
    ON lesson_conflicts(flagged_at) WHERE resolved_at IS NULL;

-- =============================================================================
-- 3. consolidation_queue: medium-confidence proposals awaiting review
-- =============================================================================

CREATE TABLE IF NOT EXISTS consolidation_queue (
    id SERIAL PRIMARY KEY,
    new_lesson_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    candidate_lesson_id INT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    proposed_action VARCHAR(20) NOT NULL CHECK (proposed_action IN ('merged', 'superseded')),
    proposed_direction VARCHAR(30),
    judge_model VARCHAR(50) NOT NULL,
    judge_confidence NUMERIC(3,2) NOT NULL,
    judge_reasoning TEXT NOT NULL,
    cosine_similarity NUMERIC(3,2) NOT NULL,
    enqueued_at TIMESTAMP DEFAULT NOW(),
    decided_at TIMESTAMP,
    decided_by VARCHAR(100),
    decision VARCHAR(20) CHECK (decision IN ('approved', 'rejected')),
    decision_note TEXT,
    CHECK (new_lesson_id <> candidate_lesson_id)
);

CREATE INDEX IF NOT EXISTS idx_consolidation_queue_pending
    ON consolidation_queue(enqueued_at) WHERE decided_at IS NULL;
```

**Step 2: Commit**

```bash
git add db/migrations/v5_consolidation.sql
git commit -m "feat(db): add v5 consolidation migration (3 tables)"
```

---

### Task 4: Deploy Migration to Local Test DB

**Files:** none (DB operation)

**Step 1: Apply migration to test database**

Run: `psql -U claude -d claude_memory_test -f db/migrations/v5_consolidation.sql`
Expected: Three `CREATE TABLE` and index messages, no errors.

**Step 2: Verify tables exist**

Run: `psql -U claude -d claude_memory_test -c "\dt lesson_merges lesson_conflicts consolidation_queue"`
Expected: Three rows listing the new tables.

**Step 3: Also apply to local dev DB (the one the server uses)**

Run: `psql -U claude -d claude_memory -f db/migrations/v5_consolidation.sql`
Expected: same three `CREATE TABLE` messages.

**Step 4: No commit — DB changes only.**

---

### Task 5: Consolidation Package Skeleton + Config

**Files:**
- Create: `src/consolidation/__init__.py`
- Create: `src/consolidation/config.py`

**Step 1: Create package init**

`src/consolidation/__init__.py`:

```python
"""v5 log-time consolidation: merge, supersede, flag contradictions."""
```

**Step 2: Create `config.py` with env-var-driven settings**

```python
"""Runtime configuration for consolidation, read from environment variables."""

import os


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")


# Kill switch
ENABLED = _bool("CONSOLIDATION_ENABLED", True)

# Candidate finder
CANDIDATE_COSINE = _float("CONSOLIDATION_CANDIDATE_COSINE", 0.85)
CANDIDATE_TOP_K = _int("CONSOLIDATION_CANDIDATE_TOP_K", 5)

# Judge
JUDGE_MODEL = os.getenv("CONSOLIDATION_JUDGE_MODEL", "claude-haiku-4-5-20251001")
JUDGE_TIMEOUT_SECONDS = _float("CONSOLIDATION_JUDGE_TIMEOUT_SECONDS", 2.0)

# Autonomy thresholds (per-action asymmetric)
AUTO_MERGE_CONFIDENCE = _float("CONSOLIDATION_AUTO_MERGE_CONFIDENCE", 0.90)
AUTO_SUPERSEDE_CONFIDENCE = _float("CONSOLIDATION_AUTO_SUPERSEDE_CONFIDENCE", 0.95)
QUEUE_MIN_CONFIDENCE = _float("CONSOLIDATION_QUEUE_MIN_CONFIDENCE", 0.60)
```

**Step 3: Commit**

```bash
git add src/consolidation/__init__.py src/consolidation/config.py
git commit -m "feat(v5): add consolidation package skeleton and config"
```

---

### Task 6: Candidate Finder

**Files:**
- Create: `src/consolidation/candidates.py`
- Create: `tests/test_candidates.py`

**Step 1: Write the failing test first**

`tests/test_candidates.py`:

```python
"""Tests for the consolidation candidate finder."""

import pytest

from src.consolidation.candidates import find_candidates


async def _make_lesson(pool, title, content, embedding, project_id=None, retired=False):
    """Insert a lesson fixture and return its id."""
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
    row = await pool.fetchrow(
        """
        INSERT INTO lessons (title, content, embedding, project_id, retired_at)
        VALUES ($1, $2, $3::vector, $4, $5)
        RETURNING id
        """,
        title, content, emb_str, project_id,
        "NOW()" if retired else None,
    )
    return row["id"]


@pytest.mark.asyncio
async def test_find_candidates_returns_top_k_above_cosine_threshold(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_CAND_%'")

    emb_a = [0.1] * 1536
    emb_b = [0.11] * 1536  # very close
    emb_c = [0.9] * 1536   # very far

    async with db_pool.acquire() as conn:
        id_a = await _make_lesson(conn, "T_CAND_A", "x", emb_a)
        id_b = await _make_lesson(conn, "T_CAND_B", "y", emb_b)
        await _make_lesson(conn, "T_CAND_C", "z", emb_c)

    results = await find_candidates(db_pool, query_embedding=emb_a,
                                    new_lesson_id=id_a, project_id=None,
                                    cosine_threshold=0.9, top_k=5)

    returned_ids = [r["id"] for r in results]
    assert id_b in returned_ids
    assert id_a not in returned_ids  # never return self


@pytest.mark.asyncio
async def test_find_candidates_excludes_retired_lessons(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_CAND_RET_%'")

    emb = [0.2] * 1536
    async with db_pool.acquire() as conn:
        _ = await _make_lesson(conn, "T_CAND_RET_NEW", "x", emb)
        await conn.execute(
            "INSERT INTO lessons (title, content, embedding, retired_at) "
            "VALUES ($1, $2, $3::vector, NOW())",
            "T_CAND_RET_OLD", "y", "[" + ",".join(str(x) for x in emb) + "]"
        )

    results = await find_candidates(db_pool, query_embedding=emb,
                                    new_lesson_id=-1, project_id=None,
                                    cosine_threshold=0.5, top_k=5)

    titles = [r["title"] for r in results]
    assert "T_CAND_RET_OLD" not in titles


@pytest.mark.asyncio
async def test_find_candidates_scopes_to_project_or_null(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_CAND_PROJ_%'")
        proj_a = await conn.fetchrow(
            "INSERT INTO projects (name) VALUES ('t_proj_a') "
            "ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id"
        )
        proj_b = await conn.fetchrow(
            "INSERT INTO projects (name) VALUES ('t_proj_b') "
            "ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id"
        )

    emb = [0.3] * 1536
    async with db_pool.acquire() as conn:
        await _make_lesson(conn, "T_CAND_PROJ_SAME", "x", emb, project_id=proj_a["id"])
        await _make_lesson(conn, "T_CAND_PROJ_OTHER", "y", emb, project_id=proj_b["id"])
        await _make_lesson(conn, "T_CAND_PROJ_NULL", "z", emb, project_id=None)

    results = await find_candidates(db_pool, query_embedding=emb,
                                    new_lesson_id=-1, project_id=proj_a["id"],
                                    cosine_threshold=0.5, top_k=5)

    titles = {r["title"] for r in results}
    assert "T_CAND_PROJ_SAME" in titles
    assert "T_CAND_PROJ_NULL" in titles
    assert "T_CAND_PROJ_OTHER" not in titles
```

**Step 2: Run the test and verify it fails**

Run: `pytest tests/test_candidates.py -v`
Expected: 3 FAILED with `ImportError` — `find_candidates` doesn't exist.

**Step 3: Implement `find_candidates`**

`src/consolidation/candidates.py`:

```python
"""Candidate finder: top-k nearest non-retired lessons above a cosine threshold."""

from typing import Any

import asyncpg


async def find_candidates(
    pool: asyncpg.Pool,
    query_embedding: list[float],
    new_lesson_id: int,
    project_id: int | None,
    cosine_threshold: float,
    top_k: int,
) -> list[dict[str, Any]]:
    """
    Return up to `top_k` lessons with cosine similarity >= threshold.

    - Excludes the new lesson itself (by id)
    - Excludes retired lessons
    - Scopes to same project_id OR NULL project on either side
    """
    emb_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    rows = await pool.fetch(
        """
        SELECT id, title, content, project_id, tags, severity,
               upvotes, downvotes,
               (1 - (embedding <=> $1::vector)) AS cosine
        FROM lessons
        WHERE embedding IS NOT NULL
          AND retired_at IS NULL
          AND id <> $2
          AND ($3::int IS NULL OR project_id = $3 OR project_id IS NULL)
          AND (1 - (embedding <=> $1::vector)) >= $4
        ORDER BY embedding <=> $1::vector
        LIMIT $5
        """,
        emb_str, new_lesson_id, project_id, cosine_threshold, top_k
    )

    return [dict(r) for r in rows]
```

**Step 4: Run tests again and verify they pass**

Run: `pytest tests/test_candidates.py -v`
Expected: 3 PASSED.

**Step 5: Commit**

```bash
git add src/consolidation/candidates.py tests/test_candidates.py
git commit -m "feat(v5): add consolidation candidate finder with tests"
```

---

### Task 7: Judge Module

**Files:**
- Create: `src/consolidation/judge.py`
- Create: `tests/test_judge.py`

**Step 1: Write the failing tests**

`tests/test_judge.py`:

```python
"""Tests for the consolidation judge."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.consolidation.judge import adjudicate, JudgeVerdict


def _mock_response(payload: dict):
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    return resp


@pytest.mark.asyncio
async def test_judge_parses_duplicate_verdict(mock_anthropic):
    mock_anthropic.messages.create.return_value = _mock_response({
        "relationship": "duplicate",
        "direction": None,
        "confidence": 0.92,
        "reasoning": "Both say the same thing."
    })

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="A'", candidate_content="x'",
        model="claude-haiku-4-5-20251001", timeout=2.0,
    )

    assert verdict.relationship == "duplicate"
    assert verdict.confidence == 0.92
    assert verdict.direction is None


@pytest.mark.asyncio
async def test_judge_parses_supersedes_with_direction(mock_anthropic):
    mock_anthropic.messages.create.return_value = _mock_response({
        "relationship": "supersedes",
        "direction": "new→existing",
        "confidence": 0.96,
        "reasoning": "New replaces old."
    })

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="B", candidate_content="y",
        model="claude-haiku-4-5-20251001", timeout=2.0,
    )

    assert verdict.relationship == "supersedes"
    assert verdict.direction == "new→existing"


@pytest.mark.asyncio
async def test_judge_returns_unrelated_on_malformed_json(mock_anthropic):
    resp = MagicMock()
    resp.content = [MagicMock(text="this is not json")]
    mock_anthropic.messages.create.return_value = resp

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="B", candidate_content="y",
        model="claude-haiku-4-5-20251001", timeout=2.0,
    )

    assert verdict.relationship == "unrelated"
    assert verdict.confidence == 0.0


@pytest.mark.asyncio
async def test_judge_returns_unrelated_on_timeout(mock_anthropic):
    async def slow(*a, **kw):
        await asyncio.sleep(5)

    mock_anthropic.messages.create.side_effect = slow

    verdict = await adjudicate(
        mock_anthropic,
        new_title="A", new_content="x",
        candidate_title="B", candidate_content="y",
        model="claude-haiku-4-5-20251001", timeout=0.1,
    )

    assert verdict.relationship == "unrelated"
    assert verdict.confidence == 0.0
    assert "timeout" in verdict.reasoning.lower()
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_judge.py -v`
Expected: 4 FAILED with `ImportError`.

**Step 3: Implement the judge**

`src/consolidation/judge.py`:

```python
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
```

**Step 4: Run tests and verify they pass**

Run: `pytest tests/test_judge.py -v`
Expected: 4 PASSED.

**Step 5: Commit**

```bash
git add src/consolidation/judge.py tests/test_judge.py
git commit -m "feat(v5): add consolidation judge with mocked-anthropic tests"
```

---

### Task 8: Actor — Routing Decision (Pure Function)

**Files:**
- Create: `src/consolidation/actor.py`
- Create: `tests/test_actor_routing.py`

The routing decision is a pure function `decide_action(verdict, config)` → `RoutingAction`. Testing it in isolation before any DB code lets us verify the threshold logic cleanly.

**Step 1: Write the failing tests**

`tests/test_actor_routing.py`:

```python
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
```

**Step 2: Run the tests, verify they fail**

Run: `pytest tests/test_actor_routing.py -v`
Expected: 7 FAILED with `ImportError`.

**Step 3: Implement `decide_action` and `RoutingAction`**

`src/consolidation/actor.py`:

```python
"""Actor: routes judge verdicts to DB mutations.

Split into:
- decide_action: pure function mapping (verdict, config) -> RoutingAction
- execute_action: async DB-mutating function that performs the routed action
"""

from enum import Enum

from src.consolidation.judge import JudgeVerdict


class RoutingAction(str, Enum):
    AUTO_MERGE = "auto_merge"
    AUTO_SUPERSEDE = "auto_supersede"
    ENQUEUE = "enqueue"
    FLAG_CONFLICT = "flag_conflict"
    IGNORE = "ignore"


def decide_action(verdict: JudgeVerdict, config) -> RoutingAction:
    """Route a judge verdict to the action the actor should take."""
    rel = verdict.relationship
    conf = verdict.confidence

    if rel == "unrelated":
        return RoutingAction.IGNORE

    if rel == "duplicate":
        if conf >= config.AUTO_MERGE_CONFIDENCE:
            return RoutingAction.AUTO_MERGE
        if conf >= config.QUEUE_MIN_CONFIDENCE:
            return RoutingAction.ENQUEUE
        return RoutingAction.IGNORE

    if rel == "supersedes":
        if conf >= config.AUTO_SUPERSEDE_CONFIDENCE:
            return RoutingAction.AUTO_SUPERSEDE
        if conf >= config.QUEUE_MIN_CONFIDENCE:
            return RoutingAction.ENQUEUE
        return RoutingAction.IGNORE

    if rel == "contradicts":
        if conf >= config.QUEUE_MIN_CONFIDENCE:
            return RoutingAction.FLAG_CONFLICT
        return RoutingAction.IGNORE

    return RoutingAction.IGNORE
```

**Step 4: Run tests, verify they pass**

Run: `pytest tests/test_actor_routing.py -v`
Expected: 7 PASSED.

**Step 5: Commit**

```bash
git add src/consolidation/actor.py tests/test_actor_routing.py
git commit -m "feat(v5): add actor routing-decision pure function with tests"
```

---

### Task 9: Actor — Auto-Merge DB Operation

**Files:**
- Modify: `src/consolidation/actor.py`
- Create: `tests/test_actor_merge.py`

**Step 1: Write the failing test for `execute_auto_merge`**

`tests/test_actor_merge.py`:

```python
"""Tests for the auto-merge DB mutation path."""

import pytest

from src.consolidation.actor import execute_auto_merge
from src.consolidation.judge import JudgeVerdict


async def _insert_lesson(conn, title, content, tags=None, severity="tip",
                         upvotes=0, downvotes=0):
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    row = await conn.fetchrow(
        """
        INSERT INTO lessons (title, content, embedding, tags, severity, upvotes, downvotes)
        VALUES ($1, $2, $3::vector, $4, $5, $6, $7)
        RETURNING id
        """,
        title, content, emb, tags or [], severity, upvotes, downvotes
    )
    return row["id"]


@pytest.mark.asyncio
async def test_auto_merge_retires_new_and_transfers_counters(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_MRG_%'")
        canonical_id = await _insert_lesson(conn, "T_MRG_CANON", "existing",
                                            upvotes=3, downvotes=1, tags=["alpha"],
                                            severity="tip")
        new_id = await _insert_lesson(conn, "T_MRG_NEW", "same thing",
                                      upvotes=0, downvotes=0, tags=["beta"],
                                      severity="critical")

    verdict = JudgeVerdict("duplicate", None, 0.94, "dupes")
    merge_id = await execute_auto_merge(
        db_pool, new_lesson_id=new_id, canonical_id=canonical_id,
        verdict=verdict, cosine=0.91, judge_model="claude-haiku-4-5-20251001",
    )

    async with db_pool.acquire() as conn:
        new_row = await conn.fetchrow("SELECT retired_at, retired_reason FROM lessons WHERE id=$1", new_id)
        canon_row = await conn.fetchrow(
            "SELECT upvotes, downvotes, tags, severity FROM lessons WHERE id=$1",
            canonical_id,
        )
        audit = await conn.fetchrow("SELECT * FROM lesson_merges WHERE id=$1", merge_id)

    assert new_row["retired_at"] is not None
    assert "merged into" in new_row["retired_reason"]
    assert canon_row["upvotes"] == 3  # new had 0 to transfer
    assert "alpha" in canon_row["tags"] and "beta" in canon_row["tags"]
    assert canon_row["severity"] == "critical"  # escalated from "tip"
    assert audit["action"] == "merged"
    assert audit["auto_decided"] is True
    assert audit["canonical_id"] == canonical_id
    assert audit["merged_id"] == new_id
```

**Step 2: Run the test, verify it fails**

Run: `pytest tests/test_actor_merge.py -v`
Expected: 1 FAILED with `ImportError`.

**Step 3: Append `execute_auto_merge` to `src/consolidation/actor.py`**

Add to the bottom of `actor.py`:

```python
import asyncpg


async def _annotate(conn, entity_type: str, entity_id: int, note: str) -> None:
    """Insert or append to an annotation. Uses the v4 annotations-append pattern."""
    from datetime import datetime, timezone
    existing = await conn.fetchrow(
        "SELECT id, note FROM annotations WHERE entity_type=$1 AND entity_id=$2",
        entity_type, entity_id,
    )
    if existing:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        combined = f"{existing['note']}\n\n---\n[{ts}] {note}"
        await conn.execute(
            "UPDATE annotations SET note=$1, updated_at=NOW() WHERE id=$2",
            combined, existing["id"],
        )
    else:
        await conn.execute(
            "INSERT INTO annotations (entity_type, entity_id, note) VALUES ($1,$2,$3)",
            entity_type, entity_id, note,
        )


async def execute_auto_merge(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    canonical_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
    decided_by: str | None = None,
    auto_decided: bool = True,
) -> int:
    """
    Retire the new lesson, transfer its counters + tags into canonical,
    repoint annotations, and write an audit row. Returns the new lesson_merges.id.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            new = await conn.fetchrow(
                "SELECT upvotes, downvotes, tags, severity FROM lessons WHERE id=$1",
                new_lesson_id,
            )
            if new is None:
                raise ValueError(f"new lesson {new_lesson_id} not found")
            new_up = new["upvotes"] or 0
            new_down = new["downvotes"] or 0

            # Retire the new lesson with a pointer to canonical
            reason = f"merged into {canonical_id}"
            await conn.execute(
                "UPDATE lessons SET retired_at=NOW(), retired_reason=$1 WHERE id=$2",
                reason, new_lesson_id,
            )

            # Transfer counters to canonical
            await conn.execute(
                """
                UPDATE lessons
                SET upvotes = COALESCE(upvotes,0) + $1,
                    downvotes = COALESCE(downvotes,0) + $2
                WHERE id = $3
                """,
                new_up, new_down, canonical_id,
            )

            # Union tags (no duplicates)
            await conn.execute(
                """
                UPDATE lessons
                SET tags = (SELECT ARRAY(SELECT DISTINCT unnest(tags || $1::text[])))
                WHERE id = $2
                """,
                new["tags"] or [], canonical_id,
            )

            # Severity escalation: canonical takes max(canonical.severity, new.severity)
            # Ordering: critical > important > tip
            await _escalate_severity(conn, canonical_id, new["severity"])

            # Repoint annotations from new -> canonical
            await conn.execute(
                "UPDATE annotations SET entity_id=$1 "
                "WHERE entity_type='lesson' AND entity_id=$2",
                canonical_id, new_lesson_id,
            )

            # Audit row
            audit = await conn.fetchrow(
                """
                INSERT INTO lesson_merges
                  (canonical_id, merged_id, action, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity, auto_decided, decided_by,
                   transferred_upvotes, transferred_downvotes)
                VALUES ($1,$2,'merged',$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id
                """,
                canonical_id, new_lesson_id, judge_model, verdict.confidence,
                verdict.reasoning, cosine, auto_decided, decided_by, new_up, new_down,
            )

            await _annotate(
                conn, "lesson", canonical_id,
                f"📎 Merged from lesson #{new_lesson_id}: {verdict.reasoning}",
            )

            return audit["id"]


_SEVERITY_ORDER = {"tip": 0, "important": 1, "critical": 2}


async def _escalate_severity(conn, canonical_id: int, incoming_severity: str) -> None:
    """Update canonical severity to the higher of (current, incoming)."""
    if not incoming_severity:
        return
    row = await conn.fetchrow("SELECT severity FROM lessons WHERE id=$1", canonical_id)
    if not row:
        return
    current = row["severity"] or "tip"
    if _SEVERITY_ORDER.get(incoming_severity, 0) > _SEVERITY_ORDER.get(current, 0):
        await conn.execute(
            "UPDATE lessons SET severity=$1 WHERE id=$2",
            incoming_severity, canonical_id,
        )
```

**Step 4: Run tests, verify pass**

Run: `pytest tests/test_actor_merge.py -v`
Expected: 1 PASSED.

**Step 5: Commit**

```bash
git add src/consolidation/actor.py tests/test_actor_merge.py
git commit -m "feat(v5): add execute_auto_merge with counter transfer and audit"
```

---

### Task 10: Actor — Auto-Supersede, Enqueue, Flag-Conflict DB Operations

**Files:**
- Modify: `src/consolidation/actor.py`
- Create: `tests/test_actor_other.py`

**Step 1: Write failing tests for the other three paths**

`tests/test_actor_other.py`:

```python
"""Tests for supersede, enqueue, and flag-conflict DB paths."""

import pytest

from src.consolidation.actor import (
    execute_auto_supersede, execute_enqueue, execute_flag_conflict,
)
from src.consolidation.judge import JudgeVerdict


async def _insert_lesson(conn, title, content, upvotes=0, downvotes=0):
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    row = await conn.fetchrow(
        "INSERT INTO lessons (title, content, embedding, upvotes, downvotes) "
        "VALUES ($1,$2,$3::vector,$4,$5) RETURNING id",
        title, content, emb, upvotes, downvotes,
    )
    return row["id"]


@pytest.mark.asyncio
async def test_auto_supersede_new_to_existing_retires_existing(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_SUP_%'")
        existing_id = await _insert_lesson(conn, "T_SUP_OLD", "old", upvotes=2)
        new_id = await _insert_lesson(conn, "T_SUP_NEW", "new")

    verdict = JudgeVerdict("supersedes", "new→existing", 0.97, "replaces")
    merge_id = await execute_auto_supersede(
        db_pool, new_lesson_id=new_id, existing_lesson_id=existing_id,
        verdict=verdict, cosine=0.88, judge_model="claude-haiku-4-5-20251001",
    )

    async with db_pool.acquire() as conn:
        existing_row = await conn.fetchrow("SELECT retired_at FROM lessons WHERE id=$1", existing_id)
        new_row = await conn.fetchrow("SELECT retired_at, upvotes FROM lessons WHERE id=$1", new_id)
        audit = await conn.fetchrow("SELECT * FROM lesson_merges WHERE id=$1", merge_id)

    assert existing_row["retired_at"] is not None  # existing retired
    assert new_row["retired_at"] is None           # new stays live
    assert new_row["upvotes"] == 2                 # counters moved from existing
    assert audit["canonical_id"] == new_id
    assert audit["merged_id"] == existing_id
    assert audit["action"] == "superseded"


@pytest.mark.asyncio
async def test_enqueue_creates_queue_row_and_annotations(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_Q_%'")
        cand_id = await _insert_lesson(conn, "T_Q_CAND", "old")
        new_id = await _insert_lesson(conn, "T_Q_NEW", "new")

    verdict = JudgeVerdict("duplicate", None, 0.72, "possibly dup")
    queue_id = await execute_enqueue(
        db_pool, new_lesson_id=new_id, candidate_lesson_id=cand_id,
        verdict=verdict, cosine=0.82, judge_model="claude-haiku-4-5-20251001",
        proposed_action="merged",
    )

    async with db_pool.acquire() as conn:
        q = await conn.fetchrow("SELECT * FROM consolidation_queue WHERE id=$1", queue_id)
        new_ann = await conn.fetchrow(
            "SELECT note FROM annotations WHERE entity_type='lesson' AND entity_id=$1", new_id)
        cand_ann = await conn.fetchrow(
            "SELECT note FROM annotations WHERE entity_type='lesson' AND entity_id=$1", cand_id)

    assert q["decision"] is None
    assert q["new_lesson_id"] == new_id
    assert q["candidate_lesson_id"] == cand_id
    assert "Consolidation pending" in new_ann["note"]
    assert "Consolidation pending" in cand_ann["note"]


@pytest.mark.asyncio
async def test_flag_conflict_creates_conflict_row_with_canonical_ordering(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T_CF_%'")
        a_id = await _insert_lesson(conn, "T_CF_A", "x")
        b_id = await _insert_lesson(conn, "T_CF_B", "y")

    verdict = JudgeVerdict("contradicts", None, 0.80, "opposite advice")
    conflict_id = await execute_flag_conflict(
        db_pool, new_lesson_id=b_id, candidate_lesson_id=a_id,
        verdict=verdict, cosine=0.81, judge_model="claude-haiku-4-5-20251001",
    )

    async with db_pool.acquire() as conn:
        c = await conn.fetchrow("SELECT * FROM lesson_conflicts WHERE id=$1", conflict_id)

    # Canonical ordering: lesson_a_id < lesson_b_id regardless of call-site order
    assert c["lesson_a_id"] == min(a_id, b_id)
    assert c["lesson_b_id"] == max(a_id, b_id)
    assert c["resolved_at"] is None
```

**Step 2: Run tests, verify they fail**

Run: `pytest tests/test_actor_other.py -v`
Expected: 3 FAILED with `ImportError`.

**Step 3: Append the three functions to `src/consolidation/actor.py`**

```python
async def execute_auto_supersede(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    existing_lesson_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
    decided_by: str | None = None,
    auto_decided: bool = True,
) -> int:
    """
    Direction is new→existing: existing is retired, new is canonical.
    Transfer existing's counters + tags to new.
    For direction existing→new, caller should use execute_auto_merge with
    new_lesson_id as the merged_id (existing is canonical).
    """
    if verdict.direction != "new→existing":
        raise ValueError(
            "execute_auto_supersede only handles direction='new→existing'; "
            "use execute_auto_merge for 'existing→new'"
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT upvotes, downvotes, tags FROM lessons WHERE id=$1",
                existing_lesson_id,
            )
            if existing is None:
                raise ValueError(f"existing lesson {existing_lesson_id} not found")
            ex_up = existing["upvotes"] or 0
            ex_down = existing["downvotes"] or 0

            reason = f"superseded by {new_lesson_id}"
            await conn.execute(
                "UPDATE lessons SET retired_at=NOW(), retired_reason=$1 WHERE id=$2",
                reason, existing_lesson_id,
            )

            await conn.execute(
                "UPDATE lessons SET upvotes=COALESCE(upvotes,0)+$1, "
                "downvotes=COALESCE(downvotes,0)+$2 WHERE id=$3",
                ex_up, ex_down, new_lesson_id,
            )

            await conn.execute(
                "UPDATE lessons SET tags=(SELECT ARRAY(SELECT DISTINCT unnest(tags || $1::text[]))) "
                "WHERE id=$2",
                existing["tags"] or [], new_lesson_id,
            )

            # Severity escalation: new takes max(new.severity, existing.severity)
            ex_sev = await conn.fetchval(
                "SELECT severity FROM lessons WHERE id=$1", existing_lesson_id,
            )
            await _escalate_severity(conn, new_lesson_id, ex_sev)

            await conn.execute(
                "UPDATE annotations SET entity_id=$1 "
                "WHERE entity_type='lesson' AND entity_id=$2",
                new_lesson_id, existing_lesson_id,
            )

            audit = await conn.fetchrow(
                """
                INSERT INTO lesson_merges
                  (canonical_id, merged_id, action, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity, auto_decided, decided_by,
                   transferred_upvotes, transferred_downvotes)
                VALUES ($1,$2,'superseded',$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id
                """,
                new_lesson_id, existing_lesson_id, judge_model, verdict.confidence,
                verdict.reasoning, cosine, auto_decided, decided_by, ex_up, ex_down,
            )

            await _annotate(
                conn, "lesson", new_lesson_id,
                f"↗ Supersedes lesson #{existing_lesson_id}: {verdict.reasoning}",
            )

            return audit["id"]


async def execute_enqueue(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    candidate_lesson_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
    proposed_action: str,  # 'merged' | 'superseded'
) -> int:
    """Create a queue row and annotate both lessons with pending notices."""
    if proposed_action not in ("merged", "superseded"):
        raise ValueError(f"invalid proposed_action: {proposed_action}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO consolidation_queue
                  (new_lesson_id, candidate_lesson_id, proposed_action,
                   proposed_direction, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                RETURNING id
                """,
                new_lesson_id, candidate_lesson_id, proposed_action,
                verdict.direction, judge_model, verdict.confidence,
                verdict.reasoning, cosine,
            )
            queue_id = row["id"]

            pending_new = (
                f"⏸ Consolidation pending (queue #{queue_id}): proposed {proposed_action} "
                f"with lesson #{candidate_lesson_id}. Confidence {verdict.confidence:.2f}. "
                f"Run approve_consolidation({queue_id}) or reject_consolidation({queue_id})."
            )
            pending_cand = (
                f"⏸ Consolidation pending (queue #{queue_id}): proposed {proposed_action} "
                f"with lesson #{new_lesson_id}. Confidence {verdict.confidence:.2f}. "
                f"Run approve_consolidation({queue_id}) or reject_consolidation({queue_id})."
            )
            await _annotate(conn, "lesson", new_lesson_id, pending_new)
            await _annotate(conn, "lesson", candidate_lesson_id, pending_cand)

            return queue_id


async def execute_flag_conflict(
    pool: asyncpg.Pool,
    new_lesson_id: int,
    candidate_lesson_id: int,
    verdict: JudgeVerdict,
    cosine: float,
    judge_model: str,
) -> int:
    """
    Insert a conflict row with canonical ordering (a_id < b_id) and annotate both.
    Returns the new lesson_conflicts.id.
    """
    a_id, b_id = sorted([new_lesson_id, candidate_lesson_id])

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO lesson_conflicts
                  (lesson_a_id, lesson_b_id, judge_model, judge_confidence,
                   judge_reasoning, cosine_similarity)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (lesson_a_id, lesson_b_id) DO UPDATE
                  SET judge_confidence = EXCLUDED.judge_confidence,
                      judge_reasoning = EXCLUDED.judge_reasoning,
                      cosine_similarity = EXCLUDED.cosine_similarity,
                      flagged_at = NOW()
                RETURNING id
                """,
                a_id, b_id, judge_model, verdict.confidence,
                verdict.reasoning, cosine,
            )
            conflict_id = row["id"]

            note_for = lambda other: (
                f"⚠ Conflicts with lesson #{other} (confidence {verdict.confidence:.2f}): "
                f"{verdict.reasoning}. Resolve via resolve_conflict({conflict_id})."
            )
            await _annotate(conn, "lesson", a_id, note_for(b_id))
            await _annotate(conn, "lesson", b_id, note_for(a_id))

            return conflict_id
```

**Step 4: Run tests, verify pass**

Run: `pytest tests/test_actor_other.py -v`
Expected: 3 PASSED.

**Step 5: Run all consolidation tests together to confirm no cross-interaction**

Run: `pytest tests/test_candidates.py tests/test_judge.py tests/test_actor_routing.py tests/test_actor_merge.py tests/test_actor_other.py -v`
Expected: 18 PASSED (3 + 4 + 7 + 1 + 3).

**Step 6: Commit**

```bash
git add src/consolidation/actor.py tests/test_actor_other.py
git commit -m "feat(v5): add supersede, enqueue, flag-conflict actor paths with tests"
```

---

### Task 11: Orchestrator — `consolidate_at_log`

**Files:**
- Create: `src/consolidation/orchestrator.py`

This glues the three components together. It is the function called from `log_lesson`. Tested via an end-to-end integration test at the next task and in manual smoke testing — no isolated unit test here, since it's thin glue and its parts are already tested.

**Step 1: Implement the orchestrator**

`src/consolidation/orchestrator.py`:

```python
"""Orchestrator: the single entrypoint called from log_lesson after insert."""

import asyncio
import logging

import asyncpg
from anthropic import AsyncAnthropic

from src.consolidation import config
from src.consolidation.candidates import find_candidates
from src.consolidation.judge import adjudicate, JudgeVerdict
from src.consolidation.actor import (
    RoutingAction, decide_action,
    execute_auto_merge, execute_auto_supersede,
    execute_enqueue, execute_flag_conflict,
)

logger = logging.getLogger(__name__)


async def _judge_pair(anthropic, new_title, new_content, cand):
    """Call the judge for one candidate and return (candidate, verdict)."""
    verdict = await adjudicate(
        anthropic,
        new_title=new_title, new_content=new_content,
        candidate_title=cand["title"], candidate_content=cand["content"],
        model=config.JUDGE_MODEL, timeout=config.JUDGE_TIMEOUT_SECONDS,
    )
    return cand, verdict


def _best_non_unrelated(pairs):
    """Highest-confidence non-unrelated verdict; ties broken by higher cosine."""
    scored = [(c, v) for c, v in pairs if v.relationship != "unrelated"]
    if not scored:
        return None
    scored.sort(key=lambda cv: (cv[1].confidence, cv[0].get("cosine", 0.0)), reverse=True)
    return scored[0]


async def consolidate_at_log(
    pool: asyncpg.Pool,
    anthropic: AsyncAnthropic,
    new_lesson_id: int,
    new_title: str,
    new_content: str,
    new_embedding: list[float],
    project_id: int | None,
) -> dict:
    """
    Run consolidation for a just-inserted lesson. Returns a summary dict:
    {
      "action_taken": "ignored" | "auto_merged" | "auto_superseded" | "queued" | "flagged",
      "candidate_count": int,
      "best_verdict": str | None,
      "confidence": float | None,
      "merge_id": int | None,       # for auto_merged/auto_superseded
      "queue_id": int | None,       # for queued
      "conflict_id": int | None,    # for flagged
    }

    Failure modes always return {"action_taken": "ignored", ...} + log a warning.
    The caller (log_lesson) treats any return as non-fatal.
    """
    if not config.ENABLED:
        return {"action_taken": "ignored", "reason": "disabled",
                "candidate_count": 0, "best_verdict": None, "confidence": None}

    try:
        cands = await find_candidates(
            pool, query_embedding=new_embedding, new_lesson_id=new_lesson_id,
            project_id=project_id, cosine_threshold=config.CANDIDATE_COSINE,
            top_k=config.CANDIDATE_TOP_K,
        )
    except Exception as e:
        logger.warning("candidate_finder failed: %s", e)
        return {"action_taken": "ignored", "reason": "finder_error",
                "candidate_count": 0, "best_verdict": None, "confidence": None}

    if not cands:
        return {"action_taken": "ignored", "reason": "no_candidates",
                "candidate_count": 0, "best_verdict": None, "confidence": None}

    # Judge all candidates in parallel
    try:
        pairs = await asyncio.gather(
            *[_judge_pair(anthropic, new_title, new_content, c) for c in cands]
        )
    except Exception as e:
        logger.warning("judge fanout failed: %s", e)
        return {"action_taken": "ignored", "reason": "judge_error",
                "candidate_count": len(cands), "best_verdict": None, "confidence": None}

    best = _best_non_unrelated(pairs)
    if best is None:
        return {"action_taken": "ignored", "reason": "all_unrelated",
                "candidate_count": len(cands), "best_verdict": "unrelated",
                "confidence": None}

    cand, verdict = best
    action = decide_action(verdict, config)
    summary = {
        "action_taken": None, "candidate_count": len(cands),
        "best_verdict": verdict.relationship, "confidence": verdict.confidence,
        "merge_id": None, "queue_id": None, "conflict_id": None,
    }

    try:
        if action == RoutingAction.IGNORE:
            summary["action_taken"] = "ignored"
        elif action == RoutingAction.AUTO_MERGE:
            merge_id = await execute_auto_merge(
                pool, new_lesson_id=new_lesson_id, canonical_id=cand["id"],
                verdict=verdict, cosine=float(cand["cosine"]),
                judge_model=config.JUDGE_MODEL,
            )
            summary.update(action_taken="auto_merged", merge_id=merge_id)
        elif action == RoutingAction.AUTO_SUPERSEDE:
            if verdict.direction == "new→existing":
                merge_id = await execute_auto_supersede(
                    pool, new_lesson_id=new_lesson_id, existing_lesson_id=cand["id"],
                    verdict=verdict, cosine=float(cand["cosine"]),
                    judge_model=config.JUDGE_MODEL,
                )
                summary.update(action_taken="auto_superseded", merge_id=merge_id)
            else:  # existing→new: existing covers new's claim — retire new as 'merged'
                merge_id = await execute_auto_merge(
                    pool, new_lesson_id=new_lesson_id, canonical_id=cand["id"],
                    verdict=verdict, cosine=float(cand["cosine"]),
                    judge_model=config.JUDGE_MODEL,
                )
                summary.update(action_taken="auto_merged", merge_id=merge_id)
        elif action == RoutingAction.ENQUEUE:
            proposed = "superseded" if verdict.relationship == "supersedes" else "merged"
            queue_id = await execute_enqueue(
                pool, new_lesson_id=new_lesson_id, candidate_lesson_id=cand["id"],
                verdict=verdict, cosine=float(cand["cosine"]),
                judge_model=config.JUDGE_MODEL, proposed_action=proposed,
            )
            summary.update(action_taken="queued", queue_id=queue_id)
        elif action == RoutingAction.FLAG_CONFLICT:
            conflict_id = await execute_flag_conflict(
                pool, new_lesson_id=new_lesson_id, candidate_lesson_id=cand["id"],
                verdict=verdict, cosine=float(cand["cosine"]),
                judge_model=config.JUDGE_MODEL,
            )
            summary.update(action_taken="flagged", conflict_id=conflict_id)
    except Exception as e:
        logger.warning("actor execution failed for action=%s: %s", action, e)
        summary["action_taken"] = "ignored"
        summary["reason"] = f"actor_error:{type(e).__name__}"

    logger.info(
        "consolidation: candidates=%d verdict=%s confidence=%s action=%s",
        summary["candidate_count"], summary["best_verdict"],
        summary["confidence"], summary["action_taken"],
    )
    return summary
```

**Step 2: Commit**

```bash
git add src/consolidation/orchestrator.py
git commit -m "feat(v5): add consolidate_at_log orchestrator"
```

---

### Task 12: Integrate Orchestrator into `log_lesson` + Add Anthropic Client to AppContext

**Files:**
- Modify: `src/server.py`
- Modify: `src/tools/lessons.py`

**Step 1: Add AsyncAnthropic to `AppContext` and initialization in `src/server.py`**

Replace the `AppContext` dataclass (currently lines 32-36):

```python
@dataclass
class AppContext:
    """Shared application resources."""
    db: asyncpg.Pool
    openai: AsyncOpenAI
    anthropic: "AsyncAnthropic"
```

Add the import at the top of `src/server.py` (after the `from openai import AsyncOpenAI` line):

```python
from anthropic import AsyncAnthropic
```

Replace the module-level client declarations (currently lines 40-42):

```python
# App-level shared resources (outlive individual MCP sessions)
_db_pool: asyncpg.Pool | None = None
_openai_client: AsyncOpenAI | None = None
_anthropic_client: AsyncAnthropic | None = None
```

Update `_ensure_pool` (currently lines 44-59) to also initialize Anthropic:

```python
async def _ensure_pool() -> tuple[asyncpg.Pool, AsyncOpenAI, AsyncAnthropic]:
    """Create or return the shared connection pool, OpenAI, and Anthropic clients."""
    global _db_pool, _openai_client, _anthropic_client
    if _db_pool is None or _db_pool._closed:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        oauth_provider.set_pool(_db_pool)
        logger.info("Database connection pool created")
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
    return _db_pool, _openai_client, _anthropic_client
```

Update `app_lifespan` to yield the third client:

```python
@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Provide shared resources to MCP tool handlers."""
    pool, openai_client, anthropic_client = await _ensure_pool()
    yield AppContext(db=pool, openai=openai_client, anthropic=anthropic_client)
```

**Step 2: Update `log_lesson` in `src/tools/lessons.py` to call the orchestrator**

Replace the existing `log_lesson` body (currently lines 31-69) with:

```python
    app = ctx.request_context.lifespan_context

    # Check if lesson with same title already exists
    existing = await app.db.fetchrow(
        "SELECT id FROM lessons WHERE title = $1",
        title
    )
    if existing:
        return json.dumps({
            "success": False,
            "lesson_id": existing["id"],
            "message": f"Lesson '{title}' already exists with id {existing['id']}"
        })

    # Get project ID if specified
    project_id = None
    if project:
        project_id = await resolve_project_id(app.db, project)

    # Generate embedding
    embedding_text = f"{title}\n{content}"
    embedding = await get_embedding(app.openai, embedding_text)
    embedding_str = format_embedding(embedding)

    # Insert lesson
    row = await app.db.fetchrow(
        """
        INSERT INTO lessons (title, content, project_id, tags, severity, embedding)
        VALUES ($1, $2, $3, $4, $5, $6::vector)
        RETURNING id
        """,
        title, content, project_id, tags or [], severity, embedding_str
    )
    lesson_id = row["id"]

    # v5: log-time consolidation. Never fatal.
    consolidation = {"action_taken": "ignored", "reason": "exception"}
    try:
        from src.consolidation.orchestrator import consolidate_at_log
        consolidation = await consolidate_at_log(
            pool=app.db,
            anthropic=app.anthropic,
            new_lesson_id=lesson_id,
            new_title=title,
            new_content=content,
            new_embedding=embedding,
            project_id=project_id,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("consolidate_at_log raised: %s", e)

    return json.dumps({
        "success": True,
        "lesson_id": lesson_id,
        "message": f"Lesson '{title}' saved successfully",
        "consolidation": consolidation,
    })
```

**Step 3: Commit**

```bash
git add src/server.py src/tools/lessons.py
git commit -m "feat(v5): wire consolidate_at_log into log_lesson and app context"
```

---

### Task 13: MCP Tools Module — Queue Management

**Files:**
- Create: `src/tools/consolidation.py`

**Step 1: Create the module with three queue tools**

```python
"""v5 consolidation MCP tools: queue management, conflict resolution, and undo."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp
from src.consolidation.actor import (
    execute_auto_merge, execute_auto_supersede, _annotate,
)
from src.consolidation.judge import JudgeVerdict


def _clear_pending_annotation_text(conn, entity_type, entity_id, queue_id):
    """Return a coroutine that strips any '⏸ Consolidation pending (queue #{Q})' text
    from the single annotation row for this entity, preserving other annotation content.
    If the pending marker was the entire annotation, delete the row."""
    marker = f"⏸ Consolidation pending (queue #{queue_id}):"

    async def _do():
        row = await conn.fetchrow(
            "SELECT id, note FROM annotations WHERE entity_type=$1 AND entity_id=$2",
            entity_type, entity_id,
        )
        if not row:
            return
        note = row["note"]
        if marker not in note:
            return
        # Strip the pending block (pending line is always a single line)
        lines = [ln for ln in note.split("\n") if marker not in ln]
        # Also drop an immediately-preceding "---" separator line if present
        cleaned = "\n".join(lines).strip()
        # Collapse double separators
        while "\n\n---\n\n---" in cleaned:
            cleaned = cleaned.replace("\n\n---\n\n---", "\n\n---")
        cleaned = cleaned.strip("\n").strip()
        # Trim trailing or leading orphan separators
        while cleaned.endswith("---"):
            cleaned = cleaned[:-3].rstrip()
        while cleaned.startswith("---"):
            cleaned = cleaned[3:].lstrip()
        if not cleaned:
            await conn.execute("DELETE FROM annotations WHERE id=$1", row["id"])
        else:
            await conn.execute(
                "UPDATE annotations SET note=$1, updated_at=NOW() WHERE id=$2",
                cleaned, row["id"],
            )

    return _do()


@mcp.tool()
async def list_pending_consolidations(
    project: str = None,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """
    List consolidation proposals awaiting human review.

    Args:
        project: Filter by project name (optional)
        limit: Maximum entries to return (default 20)
    """
    app = ctx.request_context.lifespan_context

    project_filter = ""
    params = [limit]
    if project:
        from src.helpers import resolve_project_id
        pid = await resolve_project_id(app.db, project)
        if pid is None:
            return json.dumps({"results": [], "message": f"project '{project}' not found"})
        project_filter = "AND (ln.project_id = $2 OR lc.project_id = $2)"
        params.append(pid)

    rows = await app.db.fetch(
        f"""
        SELECT q.id AS queue_id, q.new_lesson_id, q.candidate_lesson_id,
               q.proposed_action, q.proposed_direction, q.judge_confidence,
               q.judge_reasoning, q.cosine_similarity, q.enqueued_at,
               ln.title AS new_title, lc.title AS candidate_title,
               EXTRACT(EPOCH FROM (NOW() - q.enqueued_at)) / 86400.0 AS age_days
        FROM consolidation_queue q
        JOIN lessons ln ON ln.id = q.new_lesson_id
        JOIN lessons lc ON lc.id = q.candidate_lesson_id
        WHERE q.decided_at IS NULL
          {project_filter}
        ORDER BY q.enqueued_at ASC
        LIMIT $1
        """,
        *params,
    )

    results = [
        {
            "queue_id": r["queue_id"],
            "new_lesson": {"id": r["new_lesson_id"], "title": r["new_title"]},
            "candidate_lesson": {"id": r["candidate_lesson_id"], "title": r["candidate_title"]},
            "proposed_action": r["proposed_action"],
            "proposed_direction": r["proposed_direction"],
            "confidence": float(r["judge_confidence"]),
            "reasoning": r["judge_reasoning"],
            "cosine": float(r["cosine_similarity"]),
            "enqueued_at": r["enqueued_at"].isoformat(),
            "age_days": round(float(r["age_days"]), 2),
        }
        for r in rows
    ]
    return json.dumps({"pending": results, "count": len(results)})


@mcp.tool()
async def approve_consolidation(
    queue_id: int,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Approve a pending consolidation proposal. Executes the merge or supersede
    exactly as proposed and clears the pending annotations.

    Args:
        queue_id: ID of the consolidation_queue entry to approve
        reviewer: Who is approving (for audit trail)
    """
    app = ctx.request_context.lifespan_context

    q = await app.db.fetchrow(
        "SELECT * FROM consolidation_queue WHERE id=$1",
        queue_id,
    )
    if q is None:
        return json.dumps({"error": f"queue entry {queue_id} not found"})
    if q["decided_at"] is not None:
        return json.dumps({"error": f"queue entry {queue_id} already decided: {q['decision']}"})

    # Refuse if canonical side was retired after enqueue
    canonical_candidate_side = (
        q["candidate_lesson_id"] if q["proposed_action"] == "merged" or
        q["proposed_direction"] == "existing→new"
        else q["new_lesson_id"]
    )
    retired = await app.db.fetchrow(
        "SELECT retired_at FROM lessons WHERE id=$1", canonical_candidate_side,
    )
    if retired and retired["retired_at"] is not None:
        return json.dumps({
            "error": f"canonical lesson {canonical_candidate_side} has been retired; "
                     f"call reject_consolidation({queue_id}) instead",
        })

    verdict = JudgeVerdict(
        relationship="duplicate" if q["proposed_action"] == "merged" else "supersedes",
        direction=q["proposed_direction"],
        confidence=float(q["judge_confidence"]),
        reasoning=q["judge_reasoning"],
    )

    reviewer = reviewer or "unknown"

    if q["proposed_action"] == "merged":
        merge_id = await execute_auto_merge(
            app.db, new_lesson_id=q["new_lesson_id"], canonical_id=q["candidate_lesson_id"],
            verdict=verdict, cosine=float(q["cosine_similarity"]),
            judge_model=q["judge_model"], decided_by=reviewer, auto_decided=False,
        )
    else:  # 'superseded'
        if q["proposed_direction"] == "new→existing":
            merge_id = await execute_auto_supersede(
                app.db, new_lesson_id=q["new_lesson_id"], existing_lesson_id=q["candidate_lesson_id"],
                verdict=verdict, cosine=float(q["cosine_similarity"]),
                judge_model=q["judge_model"], decided_by=reviewer, auto_decided=False,
            )
        else:  # 'existing→new': retire new as merged into existing
            merge_id = await execute_auto_merge(
                app.db, new_lesson_id=q["new_lesson_id"], canonical_id=q["candidate_lesson_id"],
                verdict=verdict, cosine=float(q["cosine_similarity"]),
                judge_model=q["judge_model"], decided_by=reviewer, auto_decided=False,
            )

    # Mark queue decided + clear both pending annotations
    async with app.db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE consolidation_queue SET decided_at=NOW(), decided_by=$1, decision='approved' "
                "WHERE id=$2",
                reviewer, queue_id,
            )
            await _clear_pending_annotation_text(conn, "lesson", q["new_lesson_id"], queue_id)
            await _clear_pending_annotation_text(conn, "lesson", q["candidate_lesson_id"], queue_id)

    return json.dumps({
        "success": True,
        "queue_id": queue_id,
        "merge_id": merge_id,
        "action": q["proposed_action"],
        "reviewer": reviewer,
    })


@mcp.tool()
async def reject_consolidation(
    queue_id: int,
    reason: str = None,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Reject a pending consolidation proposal. Leaves both lessons unchanged;
    clears the pending annotations on both.

    Args:
        queue_id: ID of the consolidation_queue entry to reject
        reason: Optional explanation
        reviewer: Who is rejecting
    """
    app = ctx.request_context.lifespan_context

    q = await app.db.fetchrow("SELECT * FROM consolidation_queue WHERE id=$1", queue_id)
    if q is None:
        return json.dumps({"error": f"queue entry {queue_id} not found"})
    if q["decided_at"] is not None:
        return json.dumps({"error": f"queue entry {queue_id} already decided"})

    reviewer = reviewer or "unknown"

    async with app.db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE consolidation_queue SET decided_at=NOW(), decided_by=$1, "
                "decision='rejected', decision_note=$2 WHERE id=$3",
                reviewer, reason, queue_id,
            )
            await _clear_pending_annotation_text(conn, "lesson", q["new_lesson_id"], queue_id)
            await _clear_pending_annotation_text(conn, "lesson", q["candidate_lesson_id"], queue_id)

    return json.dumps({
        "success": True, "queue_id": queue_id, "reviewer": reviewer,
        "reason": reason,
    })
```

**Step 2: Commit**

```bash
git add src/tools/consolidation.py
git commit -m "feat(v5): add queue management tools (list/approve/reject)"
```

---

### Task 14: MCP Tools — Conflict Resolution

**Files:**
- Modify: `src/tools/consolidation.py`

**Step 1: Append `list_conflicts` and `resolve_conflict` to `src/tools/consolidation.py`**

Append at the end of the file:

```python
@mcp.tool()
async def list_conflicts(
    project: str = None,
    unresolved_only: bool = True,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """
    List flagged contradictions.

    Args:
        project: Filter by project (optional)
        unresolved_only: If True (default), show only unresolved conflicts
        limit: Maximum entries to return
    """
    app = ctx.request_context.lifespan_context

    conditions = []
    params = [limit]
    if unresolved_only:
        conditions.append("c.resolved_at IS NULL")
    if project:
        from src.helpers import resolve_project_id
        pid = await resolve_project_id(app.db, project)
        if pid is None:
            return json.dumps({"conflicts": [], "message": f"project '{project}' not found"})
        conditions.append(f"(la.project_id = ${len(params) + 1} OR lb.project_id = ${len(params) + 1})")
        params.append(pid)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    rows = await app.db.fetch(
        f"""
        SELECT c.id, c.lesson_a_id, c.lesson_b_id, c.judge_confidence,
               c.judge_reasoning, c.flagged_at,
               la.title AS a_title, lb.title AS b_title,
               EXTRACT(EPOCH FROM (NOW() - c.flagged_at)) / 86400.0 AS age_days
        FROM lesson_conflicts c
        JOIN lessons la ON la.id = c.lesson_a_id
        JOIN lessons lb ON lb.id = c.lesson_b_id
        {where_clause}
        ORDER BY c.flagged_at DESC
        LIMIT $1
        """,
        *params,
    )

    results = [
        {
            "conflict_id": r["id"],
            "lesson_a": {"id": r["lesson_a_id"], "title": r["a_title"]},
            "lesson_b": {"id": r["lesson_b_id"], "title": r["b_title"]},
            "confidence": float(r["judge_confidence"]),
            "reasoning": r["judge_reasoning"],
            "flagged_at": r["flagged_at"].isoformat(),
            "age_days": round(float(r["age_days"]), 2),
        }
        for r in rows
    ]
    return json.dumps({"conflicts": results, "count": len(results)})


@mcp.tool()
async def resolve_conflict(
    conflict_id: int,
    resolution: str,  # 'kept_a' | 'kept_b' | 'kept_both' | 'irrelevant'
    note: str = None,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Resolve a flagged contradiction.

    'kept_a' → retires lesson B (conflict resolved: A preferred)
    'kept_b' → retires lesson A (symmetric)
    'kept_both' → marks resolved; no lesson changes
    'irrelevant' → marks as false positive; no changes

    Args:
        conflict_id: ID of the lesson_conflicts row
        resolution: One of kept_a | kept_b | kept_both | irrelevant
        note: Optional explanation
        reviewer: Who resolved
    """
    if resolution not in ("kept_a", "kept_b", "kept_both", "irrelevant"):
        return json.dumps({"error": f"invalid resolution '{resolution}'"})

    app = ctx.request_context.lifespan_context
    reviewer = reviewer or "unknown"

    c = await app.db.fetchrow("SELECT * FROM lesson_conflicts WHERE id=$1", conflict_id)
    if c is None:
        return json.dumps({"error": f"conflict {conflict_id} not found"})
    if c["resolved_at"] is not None:
        return json.dumps({"error": f"conflict {conflict_id} already resolved"})

    retired_id = None
    async with app.db.acquire() as conn:
        async with conn.transaction():
            if resolution == "kept_a":
                retired_id = c["lesson_b_id"]
                await conn.execute(
                    "UPDATE lessons SET retired_at=NOW(), "
                    "retired_reason='conflict resolved: A preferred' WHERE id=$1",
                    retired_id,
                )
            elif resolution == "kept_b":
                retired_id = c["lesson_a_id"]
                await conn.execute(
                    "UPDATE lessons SET retired_at=NOW(), "
                    "retired_reason='conflict resolved: B preferred' WHERE id=$1",
                    retired_id,
                )

            await conn.execute(
                "UPDATE lesson_conflicts SET resolved_at=NOW(), resolved_by=$1, "
                "resolution=$2, resolution_note=$3 WHERE id=$4",
                reviewer, resolution, note, conflict_id,
            )

            # Clear the "⚠ Conflicts with lesson ..." annotations from both
            marker_a = f"⚠ Conflicts with lesson #{c['lesson_b_id']}"
            marker_b = f"⚠ Conflicts with lesson #{c['lesson_a_id']}"
            for lesson_id, marker in [(c["lesson_a_id"], marker_a), (c["lesson_b_id"], marker_b)]:
                row = await conn.fetchrow(
                    "SELECT id, note FROM annotations WHERE entity_type='lesson' AND entity_id=$1",
                    lesson_id,
                )
                if not row or marker not in row["note"]:
                    continue
                cleaned = "\n".join(
                    ln for ln in row["note"].split("\n") if marker not in ln
                ).strip("\n").strip()
                while cleaned.endswith("---"):
                    cleaned = cleaned[:-3].rstrip()
                while cleaned.startswith("---"):
                    cleaned = cleaned[3:].lstrip()
                if not cleaned:
                    await conn.execute("DELETE FROM annotations WHERE id=$1", row["id"])
                else:
                    await conn.execute(
                        "UPDATE annotations SET note=$1, updated_at=NOW() WHERE id=$2",
                        cleaned, row["id"],
                    )

    return json.dumps({
        "success": True, "conflict_id": conflict_id, "resolution": resolution,
        "retired_lesson_id": retired_id, "reviewer": reviewer,
    })
```

**Step 2: Commit**

```bash
git add src/tools/consolidation.py
git commit -m "feat(v5): add conflict resolution tools (list/resolve)"
```

---

### Task 15: MCP Tool — Undo Consolidation

**Files:**
- Modify: `src/tools/consolidation.py`

**Step 1: Append `undo_consolidation` to `src/tools/consolidation.py`**

```python
@mcp.tool()
async def undo_consolidation(
    merge_id: int,
    reason: str,
    reviewer: str = None,
    ctx: Context = None,
) -> str:
    """
    Reverse a previously-applied merge or supersede action.

    Un-retires the merged/superseded lesson, subtracts the transferred rating
    counters from the canonical lesson, and marks the lesson_merges row as
    reversed. Does NOT restore previously-repointed annotations.

    Args:
        merge_id: ID of the lesson_merges row to reverse
        reason: Required — why this is being reversed
        reviewer: Who is reversing
    """
    if not reason or not reason.strip():
        return json.dumps({"error": "reason is required for undo_consolidation"})

    app = ctx.request_context.lifespan_context
    reviewer = reviewer or "unknown"

    m = await app.db.fetchrow("SELECT * FROM lesson_merges WHERE id=$1", merge_id)
    if m is None:
        return json.dumps({"error": f"merge {merge_id} not found"})
    if m["reversed_at"] is not None:
        return json.dumps({"error": f"merge {merge_id} already reversed at {m['reversed_at'].isoformat()}"})

    async with app.db.acquire() as conn:
        async with conn.transaction():
            # Un-retire the merged lesson
            await conn.execute(
                "UPDATE lessons SET retired_at=NULL, retired_reason=NULL WHERE id=$1",
                m["merged_id"],
            )
            # Subtract transferred counters from canonical
            await conn.execute(
                "UPDATE lessons SET upvotes=GREATEST(COALESCE(upvotes,0) - $1, 0), "
                "downvotes=GREATEST(COALESCE(downvotes,0) - $2, 0) WHERE id=$3",
                m["transferred_upvotes"], m["transferred_downvotes"], m["canonical_id"],
            )
            # Mark the merge reversed
            await conn.execute(
                "UPDATE lesson_merges SET reversed_at=NOW(), reversed_by=$1, "
                "reversed_reason=$2 WHERE id=$3",
                reviewer, reason, merge_id,
            )
            # Annotation on canonical recording the reversal
            from src.consolidation.actor import _annotate
            await _annotate(
                conn, "lesson", m["canonical_id"],
                f"↺ Merge #{merge_id} reversed by {reviewer}: {reason}",
            )

    return json.dumps({
        "success": True, "merge_id": merge_id,
        "restored_lesson_id": m["merged_id"], "reviewer": reviewer,
    })
```

**Step 2: Commit**

```bash
git add src/tools/consolidation.py
git commit -m "feat(v5): add undo_consolidation tool"
```

---

### Task 16: Register Module + Manual Smoke Test + Deploy

**Files:**
- Modify: `src/server.py`

**Step 1: Register the new tools module**

Add to `src/server.py` in the tool-registration block (currently lines 184-194, add after the `annotations` import):

```python
import src.tools.consolidation  # noqa: E402, F401
```

**Step 2: Verify the server starts without import errors**

Run (in a separate terminal):
```bash
DATABASE_URL=postgresql://claude:claude@localhost:5432/claude_memory \
  ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  OPENAI_API_KEY=$OPENAI_API_KEY \
  CONSOLIDATION_ENABLED=true \
  python -m src.server
```

Expected: `uvicorn` starts on port 8003, no import errors in stdout.

Stop the server with Ctrl+C.

**Step 3: Run the full unit-test suite**

Run: `pytest tests/ -v`
Expected: all 18+ tests pass.

**Step 4: Manual smoke test — auto-merge path**

Start the server locally (Step 2 command). In another terminal, using curl or an MCP client:

1. Log a lesson: `log_lesson(title="SMOKE: iOS share sheet", content="Always set sharePositionOrigin on all iOS devices")`.
2. Log a near-duplicate: `log_lesson(title="SMOKE: iOS share sheet origin", content="Set sharePositionOrigin for every iOS device — required on iPad and iPhone")`.
3. Inspect response of #2. Expected: `consolidation.action_taken == "auto_merged"`, `merge_id` present.
4. Query DB: `psql -U claude -d claude_memory -c "SELECT * FROM lesson_merges WHERE canonical_id = <id from step 1>"`.
   Expected: one row, `action='merged'`, `auto_decided=true`, `judge_reasoning` populated.
5. Query annotations: `psql -U claude -d claude_memory -c "SELECT note FROM annotations WHERE entity_type='lesson' AND entity_id = <id from step 1>"`.
   Expected: a note starting with "📎 Merged from lesson #...".

**Step 5: Manual smoke test — enqueue path**

1. Set `CONSOLIDATION_AUTO_MERGE_CONFIDENCE=0.99` and restart the server to force enqueue for all dup matches.
2. Log two similar (but not identical) lessons.
3. Expected: second response has `consolidation.action_taken == "queued"`, `queue_id` present.
4. `list_pending_consolidations()` tool returns the entry.
5. `approve_consolidation(queue_id, reviewer="smoke-test")` returns success and the audit row shows `auto_decided=false`, `decided_by='smoke-test'`.

**Step 6: Manual smoke test — undo**

1. Use any `merge_id` from the smoke tests.
2. Call `undo_consolidation(merge_id=<id>, reason="smoke test", reviewer="bjhengen")`.
3. Expected: response `success=true`. DB: merged lesson has `retired_at=NULL`; `lesson_merges.reversed_at` is set.

**Step 7: Commit the registration**

```bash
git add src/server.py
git commit -m "feat(v5): register consolidation tool module"
```

---

### Task 17: Deploy to Production

**Files:** none (deployment)

**Step 1: Push to origin main**

Run: `git push origin main`
Expected: push succeeds.

**Step 2: Apply migration on production**

Run:
```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com \
  'docker cp ~/claude-memory/db/migrations/v5_consolidation.sql claude_memory_db:/tmp/ && \
   docker exec claude_memory_db psql -U claude -d claude_memory -f /tmp/v5_consolidation.sql'
```
Expected: three `CREATE TABLE` messages, no errors.

**Step 3: Pull code and rebuild the container on production**

Run:
```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com \
  'cd ~/claude-memory && git pull origin main && docker-compose build mcp'
```
Expected: image builds without errors.

**Step 4: Restart container using the v4 `docker run` workaround (docker-compose v1 ContainerConfig bug — see lesson #339)**

Run:
```bash
ssh -i ~/.ssh/AWS_FR.pem ubuntu@ec2-44-212-169-119.compute-1.amazonaws.com \
  'docker stop claude_memory_mcp && docker rm claude_memory_mcp && \
   docker run -d --name claude_memory_mcp \
     --network claude-memory_claude_memory_net \
     -p 8004:8003 \
     --env-file ~/claude-memory/.env \
     -e DATABASE_URL="postgresql://claude:$(grep POSTGRES_PASSWORD ~/claude-memory/.env | cut -d= -f2)@db:5432/claude_memory" \
     claude-memory_mcp'
```
Expected: container id echoed; container starts healthy.

**Step 5: Verify tool count via an MCP client**

Expected: 54 tools (was 48; +6 for consolidation).

**Step 6: Verify `ANTHROPIC_API_KEY` is in `~/claude-memory/.env` on prod**

Run: `ssh ... 'grep ANTHROPIC_API_KEY ~/claude-memory/.env'`
Expected: non-empty value. If missing, add it and restart the container.

**Step 7: Production smoke**

From a Claude Desktop or Claude Code session pointed at the prod server, log a lesson with a title similar to an existing one and confirm the response contains a `consolidation` field with a sensible `action_taken`.

**Step 8: No commit — deployment only.**

---

## Rollback Plan

If anything goes wrong in production:

1. **Kill switch:** set `CONSOLIDATION_ENABLED=false` in `~/claude-memory/.env` and restart. `log_lesson` will skip consolidation entirely and behave as v4.
2. **Revert code:** `git revert <range>` + redeploy.
3. **Revert schema:** the migration is additive — no schema rollback is required to disable consolidation. Tables can remain in place with `CONSOLIDATION_ENABLED=false` until removed deliberately. If forced: `DROP TABLE consolidation_queue, lesson_conflicts, lesson_merges CASCADE;`.

## What This Plan Does Not Cover

From the design doc "Out of Scope" section:

- Backlog processing for the existing ~700 lessons
- Cluster/graph summarization
- Scheduled batch runs
- Cross-entity consolidation (patterns, specs)
- Cross-project consolidation
- Held-out labeled evaluation set (pre-paper task, separate milestone)
- Annotation reversal on undo
- `re_flag_conflict` tool
