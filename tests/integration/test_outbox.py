"""Durable outbox and owner scoping against real Postgres (V16)."""
from __future__ import annotations

import asyncio

import pytest

from hermes_memory.store import OUTBOX_MAX_ATTEMPTS

pytestmark = [pytest.mark.store, pytest.mark.integration]


async def _reset(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM write_outbox")


@pytest.mark.asyncio
async def test_two_claimers_never_receive_the_same_row(db_pool, store, clean_hermes_test_db) -> None:
    await _reset(db_pool)
    for i in range(8):
        oid = await store.outbox_add("alice", {"type": "turn", "content": f"t{i}"}, "seed")
        await store.outbox_unclaim(oid)
    a, b = await asyncio.gather(
        store.outbox_claim_stale("proc-a", 5, min_age_s=0),
        store.outbox_claim_stale("proc-b", 5, min_age_s=0),
    )
    ids_a, ids_b = {i for i, _ in a}, {i for i, _ in b}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a | ids_b) == 8


@pytest.mark.asyncio
async def test_parked_rows_are_not_claimed_and_are_counted(db_pool, store, clean_hermes_test_db) -> None:
    await _reset(db_pool)
    oid = await store.outbox_add("alice", {"type": "turn", "content": "poison"}, "seed")
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE write_outbox SET claimed_by = NULL, claimed_at = NULL, attempts = $2 WHERE id = $1",
                           oid, OUTBOX_MAX_ATTEMPTS)
    assert await store.outbox_claim_stale("proc-a", 10, min_age_s=0) == []
    assert await store.outbox_counts() == {"pending": 0, "parked": 1}


@pytest.mark.asyncio
async def test_replayed_turn_with_same_outbox_id_returns_the_existing_row(db_pool, store, clean_hermes_test_db) -> None:
    first = await store.insert_turn("replay-s", "alice", "user", "short replayed turn", None,
                                    metadata={"outbox_id": "9001"})
    again = await store.insert_turn("replay-s", "alice", "user", "short replayed turn", None,
                                    metadata={"outbox_id": "9001"})
    assert first == again
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM conversations WHERE metadata->>'outbox_id' = '9001'")
    assert n == 1


@pytest.mark.asyncio
async def test_owned_turn_ids_filters_by_owner(store, clean_hermes_test_db) -> None:
    a = await store.insert_turn("own-a", "alice", "user", "alice turn about projects", None)
    b = await store.insert_turn("own-b", "bob", "user", "bob turn about projects", None)
    assert await store.owned_turn_ids([a, b], "alice") == {a}
    assert await store.owned_turn_ids([], "alice") == set()
    assert await store.latest_turn_id("alice", "own-a") == a
