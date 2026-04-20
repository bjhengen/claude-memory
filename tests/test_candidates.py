"""Tests for the consolidation candidate finder."""

import pytest

from src.consolidation.candidates import find_candidates


async def _make_lesson(pool, title, content, embedding, project_id=None, retired=False):
    """Insert a lesson fixture and return its id."""
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
    if retired:
        row = await pool.fetchrow(
            """
            INSERT INTO lessons (title, content, embedding, project_id, retired_at)
            VALUES ($1, $2, $3::vector, $4, NOW())
            RETURNING id
            """,
            title, content, emb_str, project_id,
        )
    else:
        row = await pool.fetchrow(
            """
            INSERT INTO lessons (title, content, embedding, project_id)
            VALUES ($1, $2, $3::vector, $4)
            RETURNING id
            """,
            title, content, emb_str, project_id,
        )
    return row["id"]


@pytest.mark.asyncio
async def test_find_candidates_returns_top_k_above_cosine_threshold(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_%' ESCAPE '\\'")

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
        await conn.execute("DELETE FROM lessons WHERE title LIKE 'T\\_%' ESCAPE '\\'")

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
    # Clean ALL test lesson artifacts; uniform-value embeddings in fixtures
    # collide at cosine=1.0 and would pollute top-k results otherwise.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM lessons WHERE title LIKE 'T\\_%' ESCAPE '\\' "
            "OR title LIKE 'SMOKE\\_%' ESCAPE '\\'"
        )
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
