"""Entities and V18 schema against real Postgres."""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]


@pytest.mark.asyncio
async def test_v18_schema_is_present(db_pool, clean_hermes_test_db) -> None:
    async with db_pool.acquire() as conn:
        tables = {r["t"] for r in await conn.fetch(
            "SELECT table_name AS t FROM information_schema.tables WHERE table_schema = 'public'")}
        assert {"entity", "entity_alias", "turn_entity", "verb", "verb_synonym",
                "claim_evidence", "data_migrations"} <= tables
        verbs = {r["verb"]: r["functional"] for r in await conn.fetch("SELECT verb, functional FROM verb")}
        assert verbs["lives_in"] is True and verbs["uses"] is False
        assert await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'claims' AND column_name = 'span_id'") == "YES"
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_claim_live_key'") == 0


@pytest.mark.asyncio
async def test_valid_mirrors_status(db_pool, store, clean_hermes_test_db) -> None:
    span = await store.insert_turn("s", "alice", "user", "Mirror probe turn text.", None)
    async with db_pool.acquire() as conn:
        cid = await conn.fetchval(
            "INSERT INTO claims (owner, span_id, subject, verb, object, act, status) "
            "VALUES ('alice', $1, 'A', 'uses', 'B', 'assert', 'grounded') RETURNING claim_id", span)
        assert await conn.fetchval("SELECT valid FROM claims WHERE claim_id = $1", cid) == "live"
        await conn.execute("UPDATE claims SET valid_to = now() WHERE claim_id = $1", cid)
        assert await conn.fetchval("SELECT valid FROM claims WHERE claim_id = $1", cid) == "retracted"
        await conn.execute("UPDATE claims SET status = 'unconfirmed', valid_to = NULL WHERE claim_id = $1", cid)
        assert await conn.fetchval("SELECT valid FROM claims WHERE claim_id = $1", cid) == "unknown"


import asyncio


@pytest.mark.asyncio
async def test_concurrent_creation_of_one_name_makes_one_entity(db_pool, store, clean_hermes_test_db) -> None:
    ids = await asyncio.gather(*[store.ensure_entity("alice", "Acme Corp") for _ in range(6)])
    assert len(set(ids)) == 1
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM entity WHERE owner = 'alice'") == 1


@pytest.mark.asyncio
async def test_same_name_is_two_entities_for_two_owners(store, clean_hermes_test_db) -> None:
    assert await store.ensure_entity("alice", "Acme") != await store.ensure_entity("bob", "Acme")


@pytest.mark.asyncio
async def test_first_person_resolves_to_one_self_entity(db_pool, store, clean_hermes_test_db) -> None:
    a = await store.ensure_entity("alice", "I")
    b = await store.ensure_entity("alice", "me")
    assert a == b
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT type FROM entity WHERE owner = 'alice' AND id = $1", a) == "self"


@pytest.mark.asyncio
async def test_abstained_surface_never_creates_an_entity(store, clean_hermes_test_db) -> None:
    with pytest.raises(ValueError):
        await store.ensure_entity("alice", "the project")
    assert await store.resolve_entity("alice", "it") is None


@pytest.mark.asyncio
async def test_resolution_follows_merges(db_pool, store, clean_hermes_test_db) -> None:
    old = await store.ensure_entity("alice", "Tokyo Eye")
    new = await store.ensure_entity("alice", "TE Project")
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE entity SET merged_into = $2 WHERE owner = 'alice' AND id = $1", new, old)
    assert await store.resolve_entity("alice", "TE Project") == old
