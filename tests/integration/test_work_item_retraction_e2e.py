"""Spec 5 T5.4: explicit retraction is a separate USER operation, never a
model/rule prediction. retract_work_item marks the current item
'retracted' and restores its predecessor as current if one exists,
transactionally."""
from __future__ import annotations

import pytest

from hermes_memory import extraction_worker

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t54-retract"
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


@pytest.mark.asyncio
async def test_retracting_the_sole_item_marks_it_retracted_with_no_current_pointer(
    store, clean_hermes_test_db,
):
    session_id = "s1"
    first_id = await _insert_span(store, session_id, "user", "Please build the thing.")
    last_id = await _insert_span(store, session_id, "assistant", "I built the thing.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)
    await extraction_worker._run_work(store, WORKER)

    topic_key = f"session:{session_id}"
    retracted = await store.retract_work_item(OWNER, topic_key)
    assert retracted is True

    async with store.pool.acquire() as conn:
        item = await conn.fetchrow(
            "SELECT status FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
        current = await conn.fetchrow(
            "SELECT work_item_id FROM work_item_current WHERE owner = $1 AND topic_key = $2",
            OWNER, topic_key,
        )
    assert item["status"] == "retracted"
    assert current is None, "retracting the sole item must clear the current pointer, not dangle it"


@pytest.mark.asyncio
async def test_retracting_restores_the_predecessor_as_current(store, clean_hermes_test_db):
    session_id = "s2"
    first_id = await _insert_span(store, session_id, "user", "Please build the shadow replay.")
    mid_id = await _insert_span(store, session_id, "assistant", "I built the shadow replay script.")
    await _enqueue_extract_work(store, session_id, first_id, mid_id)
    await extraction_worker._run_work(store, WORKER)

    last_id = await _insert_span(store, session_id, "assistant", "I rebuilt it with real metrics.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)
    await extraction_worker._run_work(store, WORKER)

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM work_item WHERE owner = $1 AND session_id = $2 ORDER BY id",
            OWNER, session_id,
        )
    predecessor_id, successor_id = int(rows[0]["id"]), int(rows[1]["id"])

    topic_key = f"session:{session_id}"
    retracted = await store.retract_work_item(OWNER, topic_key)
    assert retracted is True

    async with store.pool.acquire() as conn:
        successor = await conn.fetchrow("SELECT status FROM work_item WHERE id = $1", successor_id)
        predecessor = await conn.fetchrow(
            "SELECT status, superseded_by FROM work_item WHERE id = $1", predecessor_id,
        )
        current = await conn.fetchrow(
            "SELECT work_item_id FROM work_item_current WHERE owner = $1 AND topic_key = $2",
            OWNER, topic_key,
        )

    assert successor["status"] == "retracted"
    assert predecessor["status"] != "retracted", "only the current item is retracted, never its predecessor"
    assert predecessor["superseded_by"] is None, "the restored predecessor is current again, not still-superseded"
    assert current["work_item_id"] == predecessor_id


@pytest.mark.asyncio
async def test_retracting_an_unknown_topic_is_a_no_op(store, clean_hermes_test_db):
    retracted = await store.retract_work_item(OWNER, "session:does-not-exist")
    assert retracted is False


@pytest.mark.asyncio
async def test_retraction_never_inferred_from_text_only_explicit_call_does_it(
    store, clean_hermes_test_db,
):
    """A user turn that LOOKS like a retraction ("never mind, forget it")
    is classified as 'abandoned' by infer_status (T5.4's cue policy), not
    silently turned into 'retracted' -- retraction is reserved for an
    explicit, separate caller invoking retract_work_item, never inferred
    from conversation text by the rules extractor."""
    session_id = "s3"
    first_id = await _insert_span(store, session_id, "user", "Please build the thing.")
    await _insert_span(store, session_id, "assistant", "Working on it.")
    last_id = await _insert_span(store, session_id, "user", "Never mind, forget it.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)
    await extraction_worker._run_work(store, WORKER)

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert row["status"] == "abandoned"
    assert row["status"] != "retracted"
