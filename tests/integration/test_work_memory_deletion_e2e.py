"""Spec 5 T5.7: deleting a session's spans must remove its work memory
too -- work_event, work_item (and the FK-cascaded work_item_current /
work_artifact) must leave nothing behind, not dangling SET-NULL'd rows.

Deletion is exercised only via store.delete_spans (the existing,
documented span/session deletion entrypoint from store_claims.py) --
this task extends that single entrypoint rather than adding a second,
easy-to-forget deletion path for work memory.
"""
from __future__ import annotations

import pytest

from hermes_memory import extraction_worker

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t57-delete"
WORKER = "test-worker"


async def _insert_span(store, session_id: str, role: str, content: str) -> int:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, $3, $4, 'complete') RETURNING id",
            OWNER, session_id, role, content,
        )


async def _enqueue_extract_work(store, session_id: str, first_id: int, last_id: int) -> str:
    op_id = f"extract_work:{session_id}:{first_id}-{last_id}"
    await store.job_add(
        OWNER, "extract_work", op_id,
        {"owner": OWNER, "session_id": session_id, "first_span_id": first_id, "last_span_id": last_id},
        session_id=session_id,
    )
    return op_id


async def _counts(store, owner: str, session_id: str) -> dict:
    async with store.pool.acquire() as conn:
        work_event = await conn.fetchval(
            "SELECT count(*) FROM work_event WHERE owner = $1 AND session_id = $2", owner, session_id,
        )
        work_item = await conn.fetchval(
            "SELECT count(*) FROM work_item WHERE owner = $1 AND session_id = $2", owner, session_id,
        )
        work_item_current = await conn.fetchval(
            "SELECT count(*) FROM work_item_current wc JOIN work_item wi "
            "ON wc.owner = wi.owner AND wc.work_item_id = wi.id "
            "WHERE wc.owner = $1 AND wi.session_id = $2", owner, session_id,
        )
        work_artifact = await conn.fetchval(
            "SELECT count(*) FROM work_artifact wa JOIN work_item wi "
            "ON wa.owner = wi.owner AND wa.work_item_id = wi.id "
            "WHERE wa.owner = $1 AND wi.session_id = $2", owner, session_id,
        )
    return {
        "work_event": work_event, "work_item": work_item,
        "work_item_current": work_item_current, "work_artifact": work_artifact,
    }


@pytest.mark.asyncio
async def test_deleting_a_sessions_spans_leaves_no_work_memory_behind(store, clean_hermes_test_db):
    session_id = "s1"
    first_id = await _insert_span(store, session_id, "user", "Please build the thing.")
    last_id = await _insert_span(store, session_id, "assistant", "Built it, tests pass.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)
    assert await extraction_worker._run_work(store, WORKER) == 1

    async with store.pool.acquire() as conn:
        item_id = await conn.fetchval(
            "SELECT id FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
        await conn.execute(
            "INSERT INTO work_artifact (owner, work_item_id, kind, ref) VALUES ($1, $2, 'file', 'x.py')",
            OWNER, item_id,
        )
        await conn.execute(
            "INSERT INTO work_event (owner, session_id, span_id, source_event_id, tool, summary) "
            "VALUES ($1, $2, $3, 'uid-2:0', 'extra_tool', 'extra_tool: 5 chars')",
            OWNER, session_id, last_id,
        )

    before = await _counts(store, OWNER, session_id)
    assert before["work_event"] >= 1
    assert before["work_item"] == 1
    assert before["work_item_current"] == 1
    assert before["work_artifact"] == 1

    deleted = await store.delete_spans(OWNER, [first_id, last_id])
    assert deleted == 2

    after = await _counts(store, OWNER, session_id)
    assert after == {"work_event": 0, "work_item": 0, "work_item_current": 0, "work_artifact": 0}


@pytest.mark.asyncio
async def test_deleting_only_some_spans_does_not_touch_other_sessions(store, clean_hermes_test_db):
    """Deletion must be scoped to the deleted session's own rows -- a
    sibling session's work memory must be untouched."""
    session_a = "s2a"
    session_b = "s2b"
    a_first = await _insert_span(store, session_a, "user", "Build A.")
    a_last = await _insert_span(store, session_a, "assistant", "Built A.")
    await _enqueue_extract_work(store, session_a, a_first, a_last)
    assert await extraction_worker._run_work(store, WORKER) == 1

    b_first = await _insert_span(store, session_b, "user", "Build B.")
    b_last = await _insert_span(store, session_b, "assistant", "Built B.")
    await _enqueue_extract_work(store, session_b, b_first, b_last)
    assert await extraction_worker._run_work(store, WORKER) == 1

    await store.delete_spans(OWNER, [a_first, a_last])

    after_a = await _counts(store, OWNER, session_a)
    after_b = await _counts(store, OWNER, session_b)
    assert after_a == {"work_event": 0, "work_item": 0, "work_item_current": 0, "work_artifact": 0}
    assert after_b["work_item"] == 1


@pytest.mark.asyncio
async def test_null_span_id_work_event_is_removed_when_its_whole_session_is_deleted(
    store, clean_hermes_test_db,
):
    """work_event.span_id is nullable (identity resolution can fail to
    pair an event to any span) -- an event like that can't be caught by
    a span_id match. When every conversations row for that session is
    gone, its work_event rows (span-tied or not) must be too."""
    session_id = "s3"
    first_id = await _insert_span(store, session_id, "user", "Please build the thing.")
    last_id = await _insert_span(store, session_id, "assistant", "Built it.")
    async with store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO work_event (owner, session_id, span_id, source_event_id, tool, summary) "
            "VALUES ($1, $2, NULL, 'uid-3:0', 'orphan_tool', 'orphan_tool: 5 chars')",
            OWNER, session_id,
        )

    await store.delete_spans(OWNER, [first_id, last_id])

    after = await _counts(store, OWNER, session_id)
    assert after["work_event"] == 0


@pytest.mark.asyncio
async def test_null_span_id_work_event_survives_a_partial_delete_of_a_live_session(
    store, clean_hermes_test_db,
):
    """The same NULL-span_id event must NOT be swept up just because
    SOME of the session's spans were deleted -- only a fully-emptied
    session triggers the session_id-scoped cleanup."""
    session_id = "s4"
    first_id = await _insert_span(store, session_id, "user", "Please build the thing.")
    await _insert_span(store, session_id, "assistant", "Working on it.")
    await _insert_span(store, session_id, "assistant", "Built it.")
    async with store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO work_event (owner, session_id, span_id, source_event_id, tool, summary) "
            "VALUES ($1, $2, NULL, 'uid-4:0', 'orphan_tool', 'orphan_tool: 5 chars')",
            OWNER, session_id,
        )

    # Delete only one of the session's three spans -- the session is
    # still live (two spans remain).
    await store.delete_spans(OWNER, [first_id])

    after = await _counts(store, OWNER, session_id)
    assert after["work_event"] == 1
