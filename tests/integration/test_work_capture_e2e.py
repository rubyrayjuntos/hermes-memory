"""Spec 5 T5.1 end-to-end: sync_turn -> _capture_work_events -> work_event
rows, through the real provider against a throwaway DB. Uses the same
minimal-provider-instance pattern as test_spec4_assistant_claims.py."""
from __future__ import annotations

import pytest

from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider
from tests.fixtures.hermes_messages_contract import ALL_SCENARIOS

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t51-e2e"


class _FakeEmbedder:
    async def embed_text(self, text: str, *, as_query: bool = False):
        vec = [0.0] * 768
        vec[0] = 0.5
        return vec


def _make_provider(store, embedder, *, work_capture: bool) -> HybridAgeMemoryProvider:
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.config = HybridAgeConfig(dsn="unused", work_capture=work_capture)
    p.pool = store.pool
    p.store = store
    p.embedder = embedder
    p._agent_identity = OWNER
    p._session_id = "default"
    p._last_turn_id = {}
    p._write_queue = None
    p._loop = None
    p._primary_context = True
    p._recent_topics = []
    p._recent_turns = []
    p._max_topics = 8
    p._max_turns = 6
    return p


async def _write_pair_with_messages(provider, session_id, messages):
    await provider._awrite_item({
        "type": "turn", "session_id": session_id, "agent_identity": OWNER,
        "role": "user", "content": "Run the build and tell me what happened.",
        "previous_conversation_id": None,
    })
    await provider._awrite_item({
        "type": "turn", "session_id": session_id, "agent_identity": OWNER,
        "role": "assistant",
        "content": "The build ran and the tool results are captured below for review.",
        "previous_conversation_id": None, "messages": messages,
    })


@pytest.mark.asyncio
async def test_work_capture_off_by_default_writes_no_events(store, clean_hermes_test_db):
    provider = _make_provider(store, _FakeEmbedder(), work_capture=False)
    await _write_pair_with_messages(provider, "s1", ALL_SCENARIOS["simple_paired_batch"])

    async with store.pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM work_event")
    assert count == 0, "work_capture defaults off -- no behavior change until explicitly enabled"


@pytest.mark.asyncio
async def test_work_capture_on_persists_events_with_no_raw_content(store, clean_hermes_test_db):
    provider = _make_provider(store, _FakeEmbedder(), work_capture=True)
    await _write_pair_with_messages(provider, "s2", ALL_SCENARIOS["simple_paired_batch"])

    async with store.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM work_event WHERE owner = $1", OWNER)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "read_file"
    assert "DSN" not in (row["summary"] or "")
    assert row["summary"] is not None and len(row["summary"]) <= 500
    assert row["content_hash"] is not None
    assert row["span_id"] is not None  # FK'd to the assistant span that carried it


@pytest.mark.asyncio
async def test_duplicate_id_within_one_response_persists_as_two_rows(store, clean_hermes_test_db):
    provider = _make_provider(store, _FakeEmbedder(), work_capture=True)
    await _write_pair_with_messages(
        provider, "s3", ALL_SCENARIOS["duplicate_id_within_one_response"],
    )

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_event_id, content_hash FROM work_event WHERE owner = $1", OWNER,
        )
    assert len(rows) == 2
    assert len({r["source_event_id"] for r in rows}) == 2
    assert len({r["content_hash"] for r in rows}) == 2


@pytest.mark.asyncio
async def test_retrying_the_same_turn_never_duplicates_events(store, clean_hermes_test_db):
    """A redelivered/retried capture of the identical messages snapshot
    must not create duplicate work_event rows -- ON CONFLICT DO NOTHING on
    (owner, session_id, source_event_id)."""
    provider = _make_provider(store, _FakeEmbedder(), work_capture=True)
    await _write_pair_with_messages(provider, "s4", ALL_SCENARIOS["simple_paired_batch"])
    # Simulate a retry: capture the exact same messages snapshot again under the same session.
    from hermes_memory.work_capture import compute_work_events

    events, skipped, _ = compute_work_events(
        ALL_SCENARIOS["simple_paired_batch"], owner=OWNER, session_id="s4",
    )
    assert skipped == 0
    inserted_again = await store.work_event_capture_batch(events)
    assert inserted_again == 0, "retry must be a no-op, not a duplicate row"

    async with store.pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM work_event WHERE owner = $1 AND session_id = $2", OWNER, "s4",
        )
    assert count == 1
