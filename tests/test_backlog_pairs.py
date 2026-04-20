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
        # Two near-duplicates (cosine = 1.0, same direction)
        a = await _insert_lesson(conn, "T_BP_A", "close one", [0.1] * 1536)
        b = await _insert_lesson(conn, "T_BP_B", "close two", [0.1] * 1536)
        # One orthogonal neighbor: first half +, second half - → dot product with A is 0
        far_emb = [0.1] * 768 + [-0.1] * 768
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
