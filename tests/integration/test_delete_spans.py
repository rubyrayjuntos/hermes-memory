"""Deleting spans rebuilds what they supported (spec §4.2 Deletion)."""
from __future__ import annotations

import pytest

from tests.integration.claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


def lives(obj):
    return [{"subject": "Raul", "verb": "lives in", "object": obj}]


@pytest.mark.asyncio
async def test_deleting_the_newest_value_restores_the_previous_one(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), lives("NYC"))
    newest = await span(store, "alice", 2)
    await store.record_rule_claims(newest, lives("Austin"))
    assert await store.delete_spans("alice", [newest]) == 1
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]


@pytest.mark.asyncio
async def test_deleting_the_only_support_orphans_the_claim(db_pool, store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    [cid] = await store.record_rule_claims(s, lives("NYC"))
    await store.delete_spans("alice", [s])
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", cid) in ("orphaned", None)


@pytest.mark.asyncio
async def test_uptakes_are_removed_with_their_spans(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    u = await span(store, "alice", 2)
    await store.write_uptake(a, u, "unknown")
    await store.delete_spans("alice", [u])
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM uptakes") == 0


@pytest.mark.asyncio
async def test_another_owners_span_is_never_deleted(db_pool, store, clean_hermes_test_db):
    bobs = await span(store, "bob", 1)
    assert await store.delete_spans("alice", [bobs]) == 0
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations WHERE id = $1", bobs) == 1


@pytest.mark.asyncio
async def test_deleting_the_equation_span_unmerges(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [{"subject": "Acme", "verb": "uses", "object": "Atlas"}])
    await store.record_rule_claims(await span(store, "alice", 2), [{"subject": "Acme Corp", "verb": "uses", "object": "Atlas"}])
    eq = await span(store, "alice", 3)
    await store.record_alias_equation(eq, "Acme", "Acme Corp")
    await store.delete_spans("alice", [eq])
    assert await store.resolve_entity("alice", "Acme Corp") != await store.resolve_entity("alice", "Acme")
    assert await current(db_pool, "alice", "acme corp", "uses") == [("Atlas", "positive")]
