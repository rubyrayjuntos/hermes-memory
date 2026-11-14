"""Repairs retract the claims they contradict (spec §5.3)."""
from __future__ import annotations

import pytest

from tests.integration.claims_helpers import span

pytestmark = [pytest.mark.store, pytest.mark.integration]
ATLAS = {"subject": "Nightingale", "verb": "uses", "object": "Atlas"}


async def _status(db_pool, cid):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", cid)


@pytest.mark.asyncio
async def test_assistant_claim_is_unconfirmed(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    [cid] = await store.record_rule_claims(a, [ATLAS])
    assert await _status(db_pool, cid) == "unconfirmed"


@pytest.mark.asyncio
async def test_deictic_repair_retracts_the_prior_spans_claims(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    [cid] = await store.record_rule_claims(a, [ATLAS])
    u = await span(store, "alice", 2)
    assert await store.record_repair(a, u, deictic=True) == 1
    assert await _status(db_pool, cid) == "retracted"


@pytest.mark.asyncio
async def test_opposite_polarity_repair_retracts_only_the_contradicted_claim(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    atlas, borealis = await store.record_rule_claims(
        a, [ATLAS, {"subject": "Nightingale", "verb": "uses", "object": "Borealis"}])
    u = await span(store, "alice", 2)
    negated = [{**ATLAS, "polarity": "negative"}]
    assert await store.record_repair(a, u, deictic=False, new_claims=negated) == 1
    assert await _status(db_pool, atlas) == "retracted"
    assert await _status(db_pool, borealis) == "unconfirmed"


@pytest.mark.asyncio
async def test_repair_never_crosses_owners(store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    await store.record_rule_claims(a, [ATLAS])
    u = await span(store, "bob", 2)
    assert await store.record_repair(a, u, deictic=True) == 0


@pytest.mark.asyncio
async def test_repeated_repair_is_idempotent(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    await store.record_rule_claims(a, [ATLAS])
    u = await span(store, "alice", 2)
    await store.record_repair(a, u, deictic=True)
    await store.record_repair(a, u, deictic=True)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM claim_evidence WHERE act = 'retract'") == 1
