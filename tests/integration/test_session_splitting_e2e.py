"""Spec 7 T7.2/T7.3 end-to-end: multi-segment sessions, each segment its own
work_item, and already-processed (pre-Spec-7) sessions left untouched by the
live enqueue path. Test numbers match docs/hermes-memory-dev-spec.md 7.2.
"""
from __future__ import annotations

import pytest

from hermes_memory import extraction_worker
from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec7-session-splitting-e2e"
WORKER = "test-worker"


def _make_provider(store) -> HybridAgeMemoryProvider:
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.config = HybridAgeConfig(dsn="unused", work_capture=True)
    p.store = store
    return p


async def _insert_span(store, session_id: str, role: str, content: str) -> int:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, $3, $4, 'complete') RETURNING id",
            OWNER, session_id, role, content,
        )


async def _drain_all_jobs(store) -> int:
    total = 0
    while await extraction_worker._run_work(store, WORKER):
        total += 1
    return total


@pytest.mark.asyncio
async def test_8_already_processed_whole_session_item_is_left_alone(store, clean_hermes_test_db):
    session_id = "already-processed-s8"
    first_id = await _insert_span(store, session_id, "user", "do the first unrelated task")
    await _insert_span(store, session_id, "assistant", "done with the first task")
    last_id = await _insert_span(store, session_id, "user", "now do a second, unrelated task")
    await _insert_span(store, session_id, "assistant", "done with the second task")

    # Simulate a pre-Spec-7 whole-session item: inserted directly, matching
    # what a deployment running before this fix would have left in the DB
    # (bare "session:{id}" topic_key, no segment suffix). _run_work itself
    # can no longer produce this shape -- its topic_key always carries the
    # segment suffix now (T7.3) -- so a direct insert is the only way to
    # set up this precondition.
    legacy_topic_key = f"session:{session_id}"
    legacy_op_id = f"extract_work:{session_id}:{first_id}-{last_id}"
    async with store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, "
            "ask_summary, outcome_summary, status, ask_span_id, outcome_span_id, extractor_version) "
            "VALUES ($1, $2, $3, $4, 'legacy ask', 'legacy outcome', 'unknown', $5, $6, 1)",
            OWNER, session_id, legacy_topic_key, legacy_op_id, first_id, last_id,
        )

    # Now run the NEW (Spec 7) live enqueue path against this same,
    # already-fully-covered session -- it must not silently add a second
    # item for it, even though segmenting it fresh would find 2 segments.
    provider = _make_provider(store)
    await provider._aenqueue_extract_work(store, OWNER, session_id)
    await _drain_all_jobs(store)

    async with store.pool.acquire() as conn:
        final_count = await conn.fetchval(
            "SELECT count(*) FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert final_count == 1, "an already-processed session must not get silently re-split by the live path"


@pytest.mark.asyncio
async def test_9_new_session_two_unrelated_asks_makes_two_items_segment_scoped(store, clean_hermes_test_db):
    session_id = "brand-new-s9"
    id1 = await _insert_span(store, session_id, "user", "Hey, tell me about yourself!")
    id2 = await _insert_span(store, session_id, "assistant", "I'm Hermes, an AI assistant.")
    id3 = await _insert_span(store, session_id, "user", "Separately: can you check the build logs?")
    id4 = await _insert_span(store, session_id, "assistant", "Checked -- build failed on step 3.")

    provider = _make_provider(store)
    await provider._aenqueue_extract_work(store, OWNER, session_id)
    processed = await _drain_all_jobs(store)
    assert processed == 2, "two segments must enqueue two independent extract_work jobs"

    async with store.pool.acquire() as conn:
        items = await conn.fetch(
            "SELECT ask_span_id, outcome_span_id, topic_key FROM work_item "
            "WHERE owner = $1 AND session_id = $2 ORDER BY ask_span_id",
            OWNER, session_id,
        )
    assert len(items) == 2
    # Each item's ask/outcome spans never cross into the other segment.
    assert items[0]["ask_span_id"] == id1
    assert items[0]["outcome_span_id"] == id2
    assert items[1]["ask_span_id"] == id3
    assert items[1]["outcome_span_id"] == id4
    # Segment-scoped topic_keys, distinct from each other and from the
    # pre-Spec-7 whole-session shape.
    assert items[0]["topic_key"] == f"session:{session_id}:{id1}"
    assert items[1]["topic_key"] == f"session:{session_id}:{id3}"
    assert items[0]["topic_key"] != items[1]["topic_key"]


@pytest.mark.asyncio
async def test_10_retrying_the_same_segment_does_not_duplicate_its_item(store, clean_hermes_test_db):
    session_id = "retry-s10"
    await _insert_span(store, session_id, "user", "do one thing")
    await _insert_span(store, session_id, "assistant", "done")

    provider = _make_provider(store)
    await provider._aenqueue_extract_work(store, OWNER, session_id)
    await provider._aenqueue_extract_work(store, OWNER, session_id)  # simulated retry, no new spans
    await _drain_all_jobs(store)

    async with store.pool.acquire() as conn:
        job_count = await conn.fetchval(
            "SELECT count(*) FROM durable_job WHERE owner = $1 AND kind = 'extract_work' "
            "AND session_id = $2", OWNER, session_id,
        )
        item_count = await conn.fetchval(
            "SELECT count(*) FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert job_count == 1
    assert item_count == 1


@pytest.mark.asyncio
async def test_11_in_progress_sessions_tail_is_enqueued_at_session_end(store, clean_hermes_test_db):
    """_aenqueue_extract_work is only ever called from on_session_end in this
    codebase (one call site) -- so its own still-open tail segment, which
    find_segment_boundaries always returns with closed=False, is enqueued
    every time it is called, because every call IS the session-end flush.
    """
    session_id = "tail-flush-s11"
    first_id = await _insert_span(store, session_id, "user", "a single, still-open ask")

    provider = _make_provider(store)
    await provider._aenqueue_extract_work(store, OWNER, session_id)

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM durable_job WHERE owner = $1 AND kind = 'extract_work' "
            "AND session_id = $2", OWNER, session_id,
        )
    assert row is not None, "the still-open tail segment must still be enqueued at session end"
    import json
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert payload["first_span_id"] == first_id
