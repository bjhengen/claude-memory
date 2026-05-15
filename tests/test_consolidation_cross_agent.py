"""Cross-agent consolidation skip — regression tests."""

import pytest

from src.consolidation.candidates import find_candidates


@pytest.mark.asyncio
async def test_find_candidates_skips_cross_agent(db_pool):
    """A codex lesson does NOT match a claude lesson even at cosine ~1.0."""
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-claude', 'shared', $1::vector, 'claude') RETURNING id""",
        emb,
    )
    codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-codex', 'shared', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        candidates = await find_candidates(
            pool=db_pool,
            query_embedding=[0.1] * 1536,
            new_lesson_id=codex["id"],
            project_id=None,
            cosine_threshold=0.85,
            top_k=10,
            source_agent="codex",
        )
        ids = {c["id"] for c in candidates}
        assert claude["id"] not in ids
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", claude["id"], codex["id"],
        )


@pytest.mark.asyncio
async def test_find_candidates_matches_same_agent(db_pool):
    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    a = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-codex-a', 'c', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    b = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-xagent-codex-b', 'c', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        cands = await find_candidates(
            pool=db_pool, query_embedding=[0.1] * 1536,
            new_lesson_id=b["id"], project_id=None,
            cosine_threshold=0.85, top_k=10, source_agent="codex",
        )
        assert a["id"] in {c["id"] for c in cands}
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", a["id"], b["id"],
        )


@pytest.mark.asyncio
async def test_find_candidates_missing_source_agent_is_typeerror():
    """Required param: caller forgetting source_agent gets a clear failure."""
    with pytest.raises(TypeError):
        await find_candidates(
            pool=None, query_embedding=[], new_lesson_id=0,
            project_id=None, cosine_threshold=0.85, top_k=1,
        )
