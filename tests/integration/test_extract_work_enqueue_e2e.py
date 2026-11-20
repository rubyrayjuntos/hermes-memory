"""Spec 5 T5.2 end-to-end: on_session_end enqueues extract_work,
idempotently, gated behind work_capture. Calls _aenqueue_extract_work
directly (the on_session_end wrapper only adds thread/loop marshaling
that test_extraction_jobs.py already covers for the sibling
model-batch flush path)."""
from __future__ import annotations

import pytest

from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t52-e2e"


def _make_provider(store) -> HybridAgeMemoryProvider:
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.config = HybridAgeConfig(dsn="unused", work_capture=True)
    p.store = store
    return p


async def _insert_span(store, session_id: str, content: str = "placeholder content for the span"):
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, 'user', $3, 'complete') RETURNING id",
            OWNER, session_id, content,
        )


@pytest.mark.asyncio
async def test_ending_the_same_session_twice_enqueues_exactly_one_job(store, clean_hermes_test_db):
    provider = _make_provider(store)
    await _insert_span(store, "s1")
    await _insert_span(store, "s1")

    await provider._aenqueue_extract_work(store, OWNER, "s1")
    await provider._aenqueue_extract_work(store, OWNER, "s1")  # simulated retry/double callback

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM durable_job WHERE owner = $1 AND kind = 'extract_work'", OWNER,
        )
    assert len(rows) == 1, "a session ending twice with no new spans must make only one job"


@pytest.mark.asyncio
async def test_a_resumed_session_with_new_spans_gets_a_second_job(store, clean_hermes_test_db):
    """Idempotency is per-range, not a permanent lock on the session_id --
    legitimate later activity in the same session must still get captured."""
    provider = _make_provider(store)
    await _insert_span(store, "s2")
    await provider._aenqueue_extract_work(store, OWNER, "s2")

    await _insert_span(store, "s2")  # new activity in the same session, later
    await provider._aenqueue_extract_work(store, OWNER, "s2")

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM durable_job WHERE owner = $1 AND kind = 'extract_work' "
            "AND session_id = $2", OWNER, "s2",
        )
    assert len(rows) == 2, "a genuinely larger range must get its own job, not be deduped away"


@pytest.mark.asyncio
async def test_payload_is_ids_only(store, clean_hermes_test_db):
    import json

    provider = _make_provider(store)
    span_id = await _insert_span(store, "s3", content="this text must never end up in the payload")
    await provider._aenqueue_extract_work(store, OWNER, "s3")

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM durable_job WHERE owner = $1 AND kind = 'extract_work' "
            "AND session_id = $2", OWNER, "s3",
        )
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert set(payload.keys()) == {"owner", "session_id", "first_span_id", "last_span_id"}
    assert "this text must never end up in the payload" not in str(payload)
    assert payload["first_span_id"] == span_id
    assert payload["last_span_id"] == span_id


@pytest.mark.asyncio
async def test_empty_session_enqueues_nothing(store, clean_hermes_test_db):
    provider = _make_provider(store)
    await provider._aenqueue_extract_work(store, OWNER, "s-empty")

    async with store.pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM durable_job WHERE owner = $1 AND kind = 'extract_work' "
            "AND session_id = $2", OWNER, "s-empty",
        )
    assert count == 0
