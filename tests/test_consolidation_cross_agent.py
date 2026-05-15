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


@pytest.mark.asyncio
async def test_generate_pairs_returns_source_agents(db_pool):
    """generate_pairs includes a_source_agent, b_source_agent in each pair."""
    from src.consolidation.backlog import generate_pairs

    emb = "[" + ",".join(["0.1"] * 1536) + "]"
    claude = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-gp-claude', 'shared', $1::vector, 'claude') RETURNING id""",
        emb,
    )
    codex = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, embedding, source_agent)
           VALUES ('v6-gp-codex', 'shared', $1::vector, 'codex') RETURNING id""",
        emb,
    )
    try:
        pairs = await generate_pairs(pool=db_pool, cosine_threshold=0.85)
        pair = next(
            (p for p in pairs
             if {p["lesson_a_id"], p["lesson_b_id"]} == {claude["id"], codex["id"]}),
            None,
        )
        assert pair is not None
        assert {pair["a_source_agent"], pair["b_source_agent"]} == {"claude", "codex"}
    finally:
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2)", claude["id"], codex["id"],
        )


@pytest.mark.asyncio
async def test_fetch_candidate_rows_excludes_cross_agent(db_pool):
    """fetch_candidate_rows does not return rows where left/right source_agents differ."""
    from src.tools.backlog_apply import fetch_candidate_rows

    L1 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l1', 'c', 'claude') RETURNING id""",
    )
    L2 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l2', 'c', 'codex') RETURNING id""",
    )
    batch_id = "test-v6-cross-agent-skip"
    await db_pool.execute(
        """INSERT INTO backlog_analysis
           (batch_run_id, lesson_a_id, lesson_b_id, cosine_similarity, judge_model,
            verdict, confidence, reasoning, left_source_agent, right_source_agent)
           VALUES ($1, $2, $3, 0.99, 'test-model', 'duplicate', 0.95, 'test',
                   'claude', 'codex')""",
        batch_id, L1["id"], L2["id"],
    )

    L3 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l3', 'c', 'claude') RETURNING id""",
    )
    L4 = await db_pool.fetchrow(
        """INSERT INTO lessons (title, content, source_agent)
           VALUES ('v6-bx-l4', 'c', 'claude') RETURNING id""",
    )
    await db_pool.execute(
        """INSERT INTO backlog_analysis
           (batch_run_id, lesson_a_id, lesson_b_id, cosine_similarity, judge_model,
            verdict, confidence, reasoning, left_source_agent, right_source_agent)
           VALUES ($1, $2, $3, 0.99, 'test-model', 'duplicate', 0.95, 'test',
                   'claude', 'claude')""",
        batch_id, L3["id"], L4["id"],
    )

    try:
        rows = await fetch_candidate_rows(
            pool=db_pool, batch_run_id=batch_id,
            verdict_in=["duplicate"], confidence_gte=0.90,
        )
        pair_sets = [{r["lesson_a_id"], r["lesson_b_id"]} for r in rows]
        assert {L1["id"], L2["id"]} not in pair_sets, "cross-agent pair leaked"
        assert {L3["id"], L4["id"]} in pair_sets, "same-agent pair missing"
    finally:
        await db_pool.execute(
            "DELETE FROM backlog_analysis WHERE batch_run_id = $1", batch_id,
        )
        await db_pool.execute(
            "DELETE FROM lessons WHERE id IN ($1, $2, $3, $4)",
            L1["id"], L2["id"], L3["id"], L4["id"],
        )
