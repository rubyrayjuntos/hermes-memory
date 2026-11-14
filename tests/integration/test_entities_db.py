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
