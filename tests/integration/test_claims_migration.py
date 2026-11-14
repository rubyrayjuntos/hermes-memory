"""Legacy claims keep their meaning through the evidence migration (spec §7.2)."""
from __future__ import annotations

import pytest

from hermes_memory.claims_migration import legacy_check, migrate_legacy_claims
from tests.integration.claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


async def legacy(db_pool, span_id, subject, verb, obj, valid, polarity="positive", superseded_by=None):
    """A claim row shaped like V12–V15 wrote it, after V18's provisional pass."""
    status = {"live": "grounded", "retracted": "retracted"}.get(valid, "unconfirmed")
    async with db_pool.acquire() as conn:
        owner = await conn.fetchval("SELECT agent_identity FROM conversations WHERE id = $1", span_id)
        return await conn.fetchval(
            "INSERT INTO claims (owner, span_id, subject, verb, object, polarity, act, subject_canon, "
            "object_canon, status, legacy_valid, superseded_by) VALUES ($1, $2, $3, $4, $5, $6, 'assert', "
            "lower($3), lower($5), $7, $8, $9) RETURNING claim_id",
            owner, span_id, subject, verb, obj, polarity, status, valid, superseded_by)


@pytest.mark.asyncio
async def test_live_claim_stays_current(db_pool, store, clean_hermes_test_db):
    await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "live")
    await migrate_legacy_claims(store)
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "positive")]


@pytest.mark.asyncio
async def test_v15_duplicate_is_a_repeat_saying_not_a_correction(db_pool, store, clean_hermes_test_db):
    keep = await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "live")
    await legacy(db_pool, await span(store, "alice", 2), "Nightingale", "uses", "Atlas", "retracted", superseded_by=keep)
    await migrate_legacy_claims(store)
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "positive")]
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM claim_evidence WHERE act = 'retract'") == 0


@pytest.mark.asyncio
async def test_repaired_claim_stays_retracted(db_pool, store, clean_hermes_test_db):
    new = await legacy(db_pool, await span(store, "alice", 2), "Nightingale", "uses", "Atlas", "live", polarity="negative")
    await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "retracted", superseded_by=new)
    await migrate_legacy_claims(store)
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "negative")]


@pytest.mark.asyncio
async def test_retract_never_lands_on_another_owners_span(db_pool, store, clean_hermes_test_db):
    bobs = await legacy(db_pool, await span(store, "bob", 2), "Nightingale", "uses", "Atlas", "live", polarity="negative")
    alices = await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "retracted", superseded_by=bobs)
    await migrate_legacy_claims(store)
    async with db_pool.acquire() as conn:
        owners = {r["owner"] for r in await conn.fetch("SELECT owner FROM claim_evidence WHERE act = 'retract'")}
        assert owners == {"alice"}
        assert await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", alices) != "grounded"


@pytest.mark.asyncio
async def test_abstained_subject_is_orphaned_without_evidence(db_pool, store, clean_hermes_test_db):
    cid = await legacy(db_pool, await span(store, "alice", 1), "They", "uses", "Atlas", "live")
    await migrate_legacy_claims(store)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", cid) == "orphaned"


@pytest.mark.asyncio
async def test_aliases_copy_only_for_a_single_owner(db_pool, store, clean_hermes_test_db):
    await span(store, "alice", 1)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO aliases (surface_norm, canon_id, source) VALUES ('te', 'tokyo eye', 'user_span')")
    await migrate_legacy_claims(store)
    assert await store.resolve_entity("alice", "TE") == await store.resolve_entity("alice", "Tokyo Eye")


@pytest.mark.asyncio
async def test_aliases_drop_for_several_owners(db_pool, store, clean_hermes_test_db):
    await span(store, "alice", 1)
    await span(store, "bob", 2)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO aliases (surface_norm, canon_id, source) VALUES ('te', 'tokyo eye', 'user_span')")
    result = await migrate_legacy_claims(store)
    assert result["aliases_dropped"] == 1
    assert await store.resolve_entity("alice", "TE") is None


@pytest.mark.asyncio
async def test_second_run_is_skipped_and_check_is_clean(db_pool, store, clean_hermes_test_db):
    await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "live")
    await migrate_legacy_claims(store)
    assert await migrate_legacy_claims(store) == {"skipped": 1}
    async with db_pool.acquire() as conn:
        assert (await legacy_check(conn))["violations"] == []
