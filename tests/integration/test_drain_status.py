"""Fault injection: drain_status is queryable SQL, not a ledger int.

L1 insert failure leaves no row (accepted loss).
L3 flower failure stamps graph_degraded on the inserted row.
Unpassported count is the passport anti-join, not drain_status.
"""
from __future__ import annotations

import pytest

from hermes_memory.provider import HybridAgeMemoryProvider
from hermes_memory.write_outcome import DRAIN_COMPLETE, LEDGER, Kind

pytestmark = [pytest.mark.store, pytest.mark.integration]


class StubEmbedder:
    async def embed_text(self, _text):
        return [0.0] * 768


@pytest.mark.asyncio
async def test_l1_insert_failure_leaves_no_row(
    db_pool, store, clean_hermes_test_db,
) -> None:
    """Real Postgres rejects the INSERT (NULL session_id). No row, writes_failed."""

    class PgRejectInsert:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def insert_turn(self, *args, **kwargs):
            async with self._inner.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO conversations
                        (session_id, agent_identity, role, content)
                    VALUES (NULL, $1, $2, $3)
                    """,
                    "test-agent",
                    "user",
                    "hello world",
                )

    LEDGER.reset()
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider._agent_identity = "test-agent"
    provider._last_turn_id = {}
    session_id = "session-l1-absent"
    await provider._awrite_turn(
        PgRejectInsert(store),
        StubEmbedder(),
        {"session_id": session_id, "content": "hello world", "role": "user"},
    )
    async with db_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE session_id = $1",
            session_id,
        )
        any_null = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE session_id IS NULL"
        )
    assert int(n or 0) == 0
    assert int(any_null or 0) == 0
    assert LEDGER.counts()["writes_failed"] == 1


@pytest.mark.asyncio
async def test_l3_flower_failure_stamps_graph_degraded(
    db_pool, store, clean_hermes_test_db,
) -> None:
    LEDGER.reset()
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider._agent_identity = "test-agent"
    provider._last_turn_id = {}

    async def boom_flower(*args, **kwargs):
        raise RuntimeError("flower MERGE failed")

    provider._link_turn_flower = boom_flower  # type: ignore[method-assign]
    session_id = "session-l3-degraded"
    await provider._awrite_turn(
        store,
        StubEmbedder(),
        {
            "session_id": session_id,
            "content": "RateLimiter connects to VectorRouter",
            "role": "user",
        },
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, drain_status
              FROM conversations
             WHERE session_id = $1
            """,
            session_id,
        )
    assert row is not None
    assert row["drain_status"] == Kind.GRAPH_DEGRADED.value
    assert LEDGER.counts()["graph_degraded"] >= 1
    assert LEDGER.counts()["writes_failed"] == 0


@pytest.mark.asyncio
async def test_unpassported_count_ignores_drain_status(
    db_pool, store, clean_hermes_test_db,
) -> None:
    """GRAPH_DEGRADED without a passport still counts as unpassported.

    A complete row without a passport is the same V9-shaped fact.
    drain_status must not change the anti-join.
    """
    from hermes_memory.embed import vec_to_literal

    vec = vec_to_literal([0.0] * 768)
    degraded = await store.insert_turn(
        "session-gap-degraded", "test-agent", "user", "no passport yet", vec, {},
    )
    complete = await store.insert_turn(
        "session-gap-complete", "test-agent", "user", "also no passport", vec, {},
    )
    assert degraded and complete
    await store.set_drain_status(int(degraded), Kind.GRAPH_DEGRADED.value)
    await store.set_drain_status(int(complete), DRAIN_COMPLETE)
    assert await store.count_unpassported_turns() == 2
    # Adding a passport for only the degraded row drops the anti-join by one.
    from hermes_memory.extract_nouns import NounMention

    await store.write_noun_passports(
        [NounMention(label="RateLimiter", type="thing", conf=0.5, mention_index=0)],
        chunk_id=f"conv_{int(degraded)}",
        source="conversation",
        vertex_id=None,
        session_id="session-gap-degraded",
        turn_id=int(degraded),
        graph_name=store.graph_name,
    )
    assert await store.count_unpassported_turns() == 1
