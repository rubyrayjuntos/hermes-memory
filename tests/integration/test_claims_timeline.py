"""Evidence-derived claims and event-time timelines (spec §4.2, §9 timelines gate)."""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.claims_helpers import current, history, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


def lives(obj, pol="positive"):
    return {"subject": "Raul", "verb": "lives in", "object": obj, "polarity": pol}


@pytest.mark.asyncio
async def test_rule_claim_becomes_grounded_with_fact_text_and_turn_entities(db_pool, store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    [cid] = await store.record_rule_claims(s, [{"subject": "Nightingale", "verb": "uses", "object": "Atlas"}])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, valid_to, fact_text, valid FROM claims WHERE claim_id = $1", cid)
        assert (row["status"], row["valid_to"], row["valid"]) == ("grounded", None, "live")
        assert row["fact_text"] == "Nightingale uses Atlas"
        assert await conn.fetchval("SELECT count(*) FROM turn_entity WHERE span_id = $1", s) == 2


@pytest.mark.asyncio
async def test_reprocessing_a_span_adds_no_evidence(db_pool, store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    claim = [{"subject": "Nightingale", "verb": "uses", "object": "Atlas"}]
    first = await store.record_rule_claims(s, claim)
    second = await store.record_rule_claims(s, claim)
    assert first == second
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM claim_evidence") == 1


@pytest.mark.asyncio
async def test_abstained_subject_and_unknown_verb_are_skipped(store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    assert await store.record_rule_claims(s, [
        {"subject": "It", "verb": "uses", "object": "Atlas"},
        {"subject": "Raul", "verb": "juggles", "object": "Atlas"},
    ]) == []


@pytest.mark.asyncio
async def test_nyc_austin_nyc_timeline(db_pool, store, clean_hermes_test_db):
    for minute, city in [(1, "NYC"), (2, "Austin"), (3, "NYC")]:
        await store.record_rule_claims(await span(store, "alice", minute), [lives(city)])
    assert await history(db_pool, "alice", "raul", "lives_in") == [
        ("NYC", "grounded", False), ("Austin", "grounded", False), ("NYC", "grounded", True)]


@pytest.mark.asyncio
async def test_out_of_order_backfill_lands_in_its_interval(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [lives("NYC")])
    await store.record_rule_claims(await span(store, "alice", 5), [lives("NYC")])
    await store.record_rule_claims(await span(store, "alice", 3), [lives("Austin")])
    assert [o for o, _, _ in await history(db_pool, "alice", "raul", "lives_in")] == ["NYC", "Austin", "NYC"]
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]


@pytest.mark.asyncio
async def test_contradictory_polarity_resolves_to_the_newer_statement(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1),
                                   [{"subject": "Nightingale", "verb": "uses", "object": "Atlas"}])
    await store.record_rule_claims(await span(store, "alice", 2),
                                   [{"subject": "Nightingale", "verb": "uses", "object": "Atlas", "polarity": "negative"}])
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "negative")]


@pytest.mark.asyncio
async def test_owners_never_share_claims(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [lives("NYC")])
    await store.record_rule_claims(await span(store, "bob", 2), [lives("Austin")])
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]
    assert await current(db_pool, "bob", "raul", "lives_in") == [("Austin", "positive")]


@pytest.mark.asyncio
async def test_concurrent_writers_leave_one_current_row(db_pool, store, clean_hermes_test_db):
    spans = [await span(store, "alice", m) for m in range(1, 7)]
    await asyncio.gather(*[store.record_rule_claims(s, [lives("NYC" if i % 2 else "Austin")])
                           for i, s in enumerate(spans)])
    assert len(await current(db_pool, "alice", "raul", "lives_in")) == 1
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]
