"""Paste segmentation writes document evidence, never user-grounds pastes."""
from __future__ import annotations

import pytest

from hermes_memory.provider import HybridAgeMemoryProvider
from tests.integration.claims_helpers import current, span, t

pytestmark = [pytest.mark.store, pytest.mark.integration]

PASTED_EMAIL = (
    "From: alice@example.com\n"
    "Subject: ownership\n"
    "\n"
    "Bob is the owner.\n"
    "> quoted leftover\n"
    "-- \n"
    "Sent from my iPhone\n"
    "No, I meant Friday.\n"
)

LOG = "\n".join(f"2026-01-01 00:{i:02d}:00 INFO Worker uses 4GB" for i in range(60))


def _provider():
    return HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)


async def _insert(store, owner: str, minute: int, content: str, role: str = "user", session: str = "s1") -> int:
    tid = await store.insert_turn(session, owner, role, content, None, ts=t(minute))
    return int(tid)


async def _evidence(db_pool, owner: str, span_id: int | None = None):
    sql = (
        "SELECT speaker, act, subject_surface, object_surface, extractor, "
        "seg_start, seg_end FROM claim_evidence WHERE owner = $1"
    )
    args: list = [owner]
    if span_id is not None:
        sql += " AND span_id = $2"
        args.append(span_id)
    async with db_pool.acquire() as conn:
        return await conn.fetch(sql, *args)


@pytest.mark.asyncio
async def test_pasted_email_has_no_user_evidence_or_repair(store, db_pool, clean_hermes_test_db):
    prior = await _insert(store, "alice", 1, "Meeting is Thursday.", role="assistant")
    await store.record_rule_claims(prior, [{"subject": "Meeting", "verb": "is", "object": "Thursday"}])
    span_id = await _insert(store, "alice", 2, PASTED_EMAIL)
    await _provider()._reception_stage(store, span_id, "s1", "user", PASTED_EMAIL)
    rows = await _evidence(db_pool, "alice", span_id)
    assert not any(r["speaker"] == "user" for r in rows)
    assert not any(r["act"] == "retract" for r in rows)
    bob = [r for r in rows if r["subject_surface"] == "Bob" and r["speaker"] == "document"]
    assert bob
    assert all(r["extractor"] == "rules_document" for r in bob)
    async with db_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT c.status FROM claims c JOIN claim_evidence e ON e.claim_id = c.claim_id "
            "WHERE e.owner = $1 AND e.subject_surface = 'Bob' LIMIT 1",
            "alice",
        )
    assert status == "unconfirmed"


@pytest.mark.asyncio
async def test_code_fence_creates_no_claim_evidence(store, db_pool, clean_hermes_test_db):
    text = "```\nConfig uses Redis\n```"
    span_id = await _insert(store, "alice", 1, text)
    await _provider()._reception_stage(store, span_id, "s1", "user", text)
    assert await _evidence(db_pool, "alice", span_id) == []


@pytest.mark.asyncio
async def test_log_run_creates_no_user_claim(store, db_pool, clean_hermes_test_db):
    span_id = await _insert(store, "alice", 1, LOG)
    await _provider()._reception_stage(store, span_id, "s1", "user", LOG)
    rows = await _evidence(db_pool, "alice", span_id)
    assert not any(r["speaker"] == "user" for r in rows)


@pytest.mark.asyncio
async def test_mixed_turn_one_user_claim_and_document_paste(store, db_pool, clean_hermes_test_db):
    text = "Nightingale uses Postgres. Can you clean this up?\n\n" + PASTED_EMAIL
    span_id = await _insert(store, "alice", 1, text)
    await _provider()._reception_stage(store, span_id, "s1", "user", text)
    rows = await _evidence(db_pool, "alice", span_id)
    user_rows = [r for r in rows if r["speaker"] == "user" and r["act"] == "assert"]
    assert len(user_rows) == 1
    assert user_rows[0]["subject_surface"] == "Nightingale"
    pasted_claims = [r for r in rows if r["speaker"] == "document"]
    assert pasted_claims
    assert all(r["extractor"] == "rules_document" for r in pasted_claims)


@pytest.mark.asyncio
async def test_short_authored_repair_still_repairs(store, db_pool, clean_hermes_test_db):
    prior = await _insert(store, "alice", 1, "Spot is a labrador.", role="assistant")
    await store.record_rule_claims(prior, [{"subject": "Spot", "verb": "is", "object": "a labrador"}])
    text = "No, I meant Spot is a poodle."
    span_id = await _insert(store, "alice", 2, text)
    await _provider()._reception_stage(store, span_id, "s1", "user", text)
    async with db_pool.acquire() as conn:
        uptake = await conn.fetchval(
            "SELECT value FROM uptakes WHERE next_span_id = $1", span_id)
    assert uptake == "repaired"


@pytest.mark.asyncio
async def test_document_claims_are_owner_scoped(store, db_pool, clean_hermes_test_db):
    text = PASTED_EMAIL
    span_id = await _insert(store, "alice", 1, text)
    await _provider()._reception_stage(store, span_id, "s1", "user", text)
    await span(store, "bob", 2)
    bob_rows = await _evidence(db_pool, "bob")
    alice_docs = [r for r in await _evidence(db_pool, "alice") if r["speaker"] == "document"]
    assert alice_docs
    assert bob_rows == []
    seen = await store.claims_by_ids("bob", [
        int(r["claim_id"]) for r in await _claim_ids(db_pool, "alice")
    ])
    assert seen == []


async def _claim_ids(db_pool, owner: str):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT claim_id FROM claims WHERE owner = $1", owner)


@pytest.mark.asyncio
async def test_document_evidence_never_closes_user_interval(store, db_pool, clean_hermes_test_db):
    user_span = await span(store, "alice", 1)
    await store.record_rule_claims(
        user_span, [{"subject": "Raul", "verb": "lives in", "object": "NYC"}])
    doc_span = await span(store, "alice", 2)
    await store.record_rule_claims(
        doc_span, [{"subject": "Raul", "verb": "lives in", "object": "Austin"}],
        speaker="document", extractor="rules_document")
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]
