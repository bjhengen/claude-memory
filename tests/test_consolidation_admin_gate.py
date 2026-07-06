"""Mutating consolidation tools must require the 'admin' scope.

Until the 2026-07-06 review (P0.4), resolve_conflict was the only gated
consolidation tool — approve/reject/undo_consolidation and
apply_backlog_batch executed for any authenticated caller.
"""

from unittest.mock import MagicMock

import json
import pytest

from src.identity import Identity, set_identity, reset_identity
from src.server import AppContext
from src.tools.backlog_apply import apply_backlog_batch
from src.tools.consolidation import (
    approve_consolidation,
    reject_consolidation,
    undo_consolidation,
)


@pytest.fixture(autouse=True)
def _clean_identity():
    reset_identity()
    yield
    reset_identity()


def _ctx(db_pool):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        db=db_pool, openai=MagicMock(), anthropic=MagicMock(),
    )
    return ctx


def _admin():
    set_identity(Identity(
        family="claude", client_id="apikey:1",
        scopes=["read", "write", "admin"], source="apikey",
    ))


@pytest.mark.asyncio
async def test_approve_consolidation_requires_admin(db_pool):
    with pytest.raises(PermissionError):
        await approve_consolidation(queue_id=999999999, ctx=_ctx(db_pool))


@pytest.mark.asyncio
async def test_reject_consolidation_requires_admin(db_pool):
    with pytest.raises(PermissionError):
        await reject_consolidation(queue_id=999999999, ctx=_ctx(db_pool))


@pytest.mark.asyncio
async def test_undo_consolidation_requires_admin(db_pool):
    with pytest.raises(PermissionError):
        await undo_consolidation(
            merge_id=999999999, reason="gate test", ctx=_ctx(db_pool),
        )


@pytest.mark.asyncio
async def test_apply_backlog_batch_requires_admin(db_pool):
    with pytest.raises(PermissionError):
        await apply_backlog_batch(
            batch_run_id="gate-test-nonexistent", ctx=_ctx(db_pool),
        )


@pytest.mark.asyncio
async def test_admin_passes_all_gates(db_pool):
    """With admin scope the gate opens: nonexistent IDs reach normal
    not-found handling instead of raising PermissionError."""
    _admin()
    result = await approve_consolidation(queue_id=999999999, ctx=_ctx(db_pool))
    assert "error" in json.loads(result)

    result = await reject_consolidation(queue_id=999999999, ctx=_ctx(db_pool))
    assert "error" in json.loads(result)

    result = await undo_consolidation(
        merge_id=999999999, reason="gate test", ctx=_ctx(db_pool),
    )
    assert "error" in json.loads(result)

    result = await apply_backlog_batch(
        batch_run_id="gate-test-nonexistent", ctx=_ctx(db_pool),
    )
    # Preview of an unknown batch is a valid empty preview or an error —
    # either way it must get past the gate without raising.
    assert isinstance(json.loads(result), dict)
