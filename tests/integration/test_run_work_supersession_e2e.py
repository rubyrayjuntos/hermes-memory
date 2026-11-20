"""Spec 5 T5.3 completion: owner/topic-locked supersession.

T5.3's own spec text requires this explicitly ("Under an owner/topic
lock, insert the successor, upsert work_item_current, and mark the
predecessor superseded in the same transaction") -- the earlier T5.3
commit left it unbuilt (an unconditional, unlocked upsert). This file
closes that gap; it does not belong to a separate task number.
"""
from __future__ import annotations

import pytest

from hermes_memory import extraction_worker

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t53-supersede"
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
async def test_a_grown_range_supersedes_the_prior_work_item_for_the_same_topic(
    store, clean_hermes_test_db,
):
    session_id = "s1"
    first_id = await _insert_span(store, session_id, "user", "Please build the shadow replay.")
    mid_id = await _insert_span(store, session_id, "assistant", "I built the shadow replay script.")
    await _enqueue_extract_work(store, session_id, first_id, mid_id)
    assert await extraction_worker._run_work(store, WORKER) == 1

    async with store.pool.acquire() as conn:
        first_item = await conn.fetchrow(
            "SELECT id, superseded_by FROM work_item WHERE owner = $1 AND session_id = $2",
            OWNER, session_id,
        )
    assert first_item["superseded_by"] is None

    # The session grows: a second extract_work job covers a larger range.
    last_id = await _insert_span(
        store, session_id, "assistant", "I rebuilt it on shadow-replay-v2 with real metrics.",
    )
    await _enqueue_extract_work(store, session_id, first_id, last_id)
    assert await extraction_worker._run_work(store, WORKER) == 1

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, superseded_by FROM work_item WHERE owner = $1 AND session_id = $2 ORDER BY id",
            OWNER, session_id,
        )
        current = await conn.fetchrow(
            "SELECT work_item_id FROM work_item_current WHERE owner = $1 AND topic_key = $2",
            OWNER, f"session:{session_id}",
        )

    assert len(rows) == 2
    old_item, new_item = rows[0], rows[1]
    assert old_item["superseded_by"] == new_item["id"], (
        "the predecessor must point at its successor once a later range is processed"
    )
    assert new_item["superseded_by"] is None
    assert current["work_item_id"] == new_item["id"], (
        "work_item_current must point at the newest item, not the superseded one"
    )


@pytest.mark.asyncio
async def test_superseded_predecessor_stays_queryable_with_its_own_fields_intact(
    store, clean_hermes_test_db,
):
    """Supersession must never delete or blank out the predecessor row --
    only point it at its successor."""
    session_id = "s2"
    first_id = await _insert_span(store, session_id, "user", "Please build the shadow replay.")
    mid_id = await _insert_span(store, session_id, "assistant", "I built the shadow replay script.")
    await _enqueue_extract_work(store, session_id, first_id, mid_id)
    await extraction_worker._run_work(store, WORKER)

    async with store.pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT ask_summary, status FROM work_item WHERE owner = $1 AND session_id = $2",
            OWNER, session_id,
        )

    last_id = await _insert_span(store, session_id, "assistant", "I rebuilt it with real metrics.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)
    await extraction_worker._run_work(store, WORKER)

    async with store.pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT ask_summary, status FROM work_item WHERE owner = $1 AND session_id = $2 "
            "ORDER BY id LIMIT 1",
            OWNER, session_id,
        )
    assert after["ask_summary"] == before["ask_summary"]
    assert after["status"] == before["status"]
