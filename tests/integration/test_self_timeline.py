"""Self-entity functional timelines through first-person extraction (Spec 2)."""
from __future__ import annotations

import pytest

from hermes_memory.extract_publish import publish_rules
from hermes_memory.extraction_jobs import job_specs
from hermes_memory.reception import extract_assertions, extract_self_assertions
from tests.integration.claims_helpers import current, span, t

pytestmark = [pytest.mark.store, pytest.mark.integration]


async def _ingest_self(store, owner: str, minute: int, text: str, session: str = "s1") -> int:
    tid = await store.insert_turn(session, owner, "user", text, None, ts=t(minute))
    claims = extract_self_assertions(text)
    if claims:
        await store.record_rule_claims(int(tid), claims)
    return int(tid)


async def current_self(db_pool, owner: str, verb: str) -> list[tuple[str, str]]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.object, c.polarity FROM claims c "
            "JOIN entity s ON s.owner = c.owner AND s.id = c.subject_id "
            "WHERE c.owner = $1 AND s.type = 'self' AND c.verb = $2 "
            "AND c.status = 'grounded' AND c.valid_to IS NULL ORDER BY c.claim_id",
            owner, verb)
    return [(r["object"], r["polarity"]) for r in rows]


async def history_self(db_pool, owner: str, verb: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.object, c.status, c.valid_to IS NULL AS is_current, c.valid_from, c.valid_to "
            "FROM claims c JOIN entity s ON s.owner = c.owner AND s.id = c.subject_id "
            "WHERE c.owner = $1 AND s.type = 'self' AND c.verb = $2 AND c.status = 'grounded' "
            "ORDER BY c.valid_from",
            owner, verb)
    return rows


@pytest.mark.asyncio
async def test_nyc_austin_nyc_through_extraction(db_pool, store, clean_hermes_test_db):
    await _ingest_self(store, "alice", 1, "I live in NYC.")
    await _ingest_self(store, "alice", 2, "I moved to Austin last week.")
    await _ingest_self(store, "alice", 3, "I'm back in NYC.")
    assert await current_self(db_pool, "alice", "lives_in") == [("NYC", "positive")]
    hist = [(r["object"], r["status"], r["is_current"]) for r in await history_self(db_pool, "alice", "lives_in")]
    assert hist == [
        ("NYC", "grounded", False),
        ("Austin", "grounded", False),
        ("NYC", "grounded", True),
    ]


@pytest.mark.asyncio
async def test_negative_works_for_closes_prior_and_does_not_invent_without_prior(
    db_pool, store, clean_hermes_test_db,
):
    await _ingest_self(store, "alice", 1, "I work at Acme")
    assert await current_self(db_pool, "alice", "works_for") == [("Acme", "positive")]
    await _ingest_self(store, "alice", 2, "I don't work at Acme anymore")
    assert await current_self(db_pool, "alice", "works_for") == []
    rows = await history_self(db_pool, "alice", "works_for")
    closed = [r for r in rows if r["object"] == "Acme" and r["valid_to"] is not None]
    assert closed, "prior matching functional value must close the interval"

    await _ingest_self(store, "bob", 1, "I don't work at Globex anymore")
    assert await current_self(db_pool, "bob", "works_for") == []
    bob_pos = [
        r for r in await history_self(db_pool, "bob", "works_for")
        if r["object"] == "Globex" and r["is_current"]
    ]
    assert bob_pos == [], "do not manufacture a positive claim when no prior value"


@pytest.mark.asyncio
async def test_home_is_nyc_remains_nonfunctional_is(db_pool, store, clean_hermes_test_db):
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT functional FROM verb WHERE verb = 'is'") is False
    s1 = await span(store, "alice", 1)
    await store.record_rule_claims(s1, extract_assertions("Home is NYC"))
    s2 = await span(store, "alice", 2)
    await store.record_rule_claims(s2, extract_assertions("Home is Austin"))
    assert set(await current(db_pool, "alice", "home", "is")) == {
        ("NYC", "positive"),
        ("Austin", "positive"),
    }


@pytest.mark.asyncio
async def test_self_fact_owner_a_cannot_alter_owner_b(db_pool, store, clean_hermes_test_db):
    await _ingest_self(store, "alice", 1, "I live in NYC.")
    await _ingest_self(store, "bob", 1, "I live in Austin.")
    assert await current_self(db_pool, "alice", "lives_in") == [("NYC", "positive")]
    assert await current_self(db_pool, "bob", "lives_in") == [("Austin", "positive")]
    await _ingest_self(store, "alice", 2, "I moved to Denver.")
    assert await current_self(db_pool, "alice", "lives_in") == [("Denver", "positive")]
    assert await current_self(db_pool, "bob", "lives_in") == [("Austin", "positive")]
    alice_hist = [r["object"] for r in await history_self(db_pool, "alice", "lives_in")]
    assert "Austin" not in alice_hist


@pytest.mark.asyncio
async def test_publish_rules_extracts_self_lives_in(db_pool, store, clean_hermes_test_db):
    span_id = await span(store, "alice", 1)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET content = $2 WHERE id = $1", span_id, "I live in NYC.",
        )
    kind, op_id, payload = job_specs(span_id)[0]
    await store.job_add("alice", kind, op_id, payload)
    [(job_id, fence, _)] = await store.job_claim(kind, "w1", 1, lease_s=60)

    async def embed(texts):
        return [[0.01] * 768 for _ in texts]

    async with db_pool.acquire() as conn:
        ids = await publish_rules(conn, embed, job_id, fence)
    assert ids
    assert await current_self(db_pool, "alice", "lives_in") == [("NYC", "positive")]
