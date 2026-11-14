"""Durable outbox and owner scoping against real Postgres (V16)."""
from __future__ import annotations

import asyncio

import pytest

from hermes_memory.store import OUTBOX_MAX_ATTEMPTS

pytestmark = [pytest.mark.store, pytest.mark.integration]


async def _reset(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM durable_job WHERE kind = 'capture'")


@pytest.mark.asyncio
async def test_two_claimers_never_receive_the_same_row(db_pool, store, clean_hermes_test_db) -> None:
    await _reset(db_pool)
    for i in range(8):
        oid, fence = await store.outbox_add("alice", {"type": "turn", "content": f"t{i}"}, "seed", f"op-{i}")
        await store.outbox_unclaim(oid, fence)
    a, b = await asyncio.gather(
        store.outbox_claim_stale("proc-a", 5, min_age_s=0),
        store.outbox_claim_stale("proc-b", 5, min_age_s=0),
    )
    ids_a, ids_b = {i for i, _, _ in a}, {i for i, _, _ in b}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a | ids_b) == 8


@pytest.mark.asyncio
async def test_parked_rows_are_not_claimed_and_are_counted(db_pool, store, clean_hermes_test_db) -> None:
    await _reset(db_pool)
    oid, _ = await store.outbox_add("alice", {"type": "turn", "content": "poison"}, "seed", "op-poison")
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL, "
                           "attempts = $2 WHERE id = $1", oid, OUTBOX_MAX_ATTEMPTS)
    assert await store.outbox_claim_stale("proc-a", 10, min_age_s=0) == []
    assert await store.outbox_counts() == {"pending": 0, "parked": 1}


@pytest.mark.asyncio
async def test_concurrent_writes_of_one_op_store_one_row(db_pool, store, clean_hermes_test_db) -> None:
    """Live path and replay racing on the same captured write: the database keeps one."""
    results = await asyncio.gather(*[
        store.insert_turn("race-s", "alice", "user", "short raced turn", None,
                          metadata={"op_id": "op-race"}, include_inserted=True)
        for _ in range(6)
    ])
    assert len({tid for tid, _ in results}) == 1
    assert sum(1 for _, inserted in results if inserted) == 1
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM conversations WHERE metadata->>'op_id' = 'op-race'")
    assert n == 1


@pytest.mark.asyncio
async def test_outbox_record_is_idempotent_by_op_id(db_pool, store, clean_hermes_test_db) -> None:
    await _reset(db_pool)
    first = await store.outbox_add("alice", {"type": "turn"}, "proc-a", "op-same")
    second = await store.outbox_add("alice", {"type": "turn"}, "proc-a", "op-same")
    assert first == second
    assert await store.outbox_counts() == {"pending": 1, "parked": 0}


@pytest.mark.asyncio
async def test_a_stale_worker_cannot_settle_a_row_someone_else_reclaimed(db_pool, store, clean_hermes_test_db) -> None:
    await _reset(db_pool)
    oid, fence_a = await store.outbox_add("alice", {"type": "turn"}, "proc-a", "op-lease")
    async with db_pool.acquire() as conn:  # proc-a's lease has expired
        await conn.execute("UPDATE durable_job SET lease_expires_at = now() - interval '1 second', "
                           "created_at = now() - interval '1 hour' WHERE id = $1", oid)
    [(rid, fence_b, _)] = await store.outbox_claim_stale("proc-b", 5)
    assert rid == oid and fence_b > fence_a
    assert await store.outbox_done(oid, fence_a) is False  # stale worker: no-op
    assert await store.outbox_unclaim(oid, fence_a) is False
    assert await store.outbox_done(oid, fence_b) is True


@pytest.mark.asyncio
async def test_previous_turn_before_uses_event_time_not_insertion_order(store, clean_hermes_test_db) -> None:
    t1 = await store.insert_turn("order-s", "alice", "user", "first by event time", None,
                                 ts="2026-11-14T10:00:00+00:00")
    t3 = await store.insert_turn("order-s", "alice", "user", "third by event time", None,
                                 ts="2026-11-14T10:02:00+00:00")
    # The middle turn is replayed last; its predecessor is t1, not t3.
    assert await store.previous_turn_before("alice", "order-s", "2026-11-14T10:01:00+00:00") == t1
    assert await store.previous_turn_before("alice", "order-s", "2026-11-14T10:03:00+00:00") == t3
    assert await store.previous_turn_before("bob", "order-s", "2026-11-14T10:03:00+00:00") is None


@pytest.mark.asyncio
async def test_owned_turn_ids_filters_by_owner(store, clean_hermes_test_db) -> None:
    a = await store.insert_turn("own-a", "alice", "user", "alice turn about projects", None)
    b = await store.insert_turn("own-b", "bob", "user", "bob turn about projects", None)
    assert await store.owned_turn_ids([a, b], "alice") == {a}
    assert await store.owned_turn_ids([], "alice") == set()
