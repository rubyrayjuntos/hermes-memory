"""Fill fact vectors for current claims (spec §5.2 steps 3 and 6)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hermes_memory.store_claims import claims_missing_vectors, write_fact_vectors

pytestmark = [pytest.mark.store, pytest.mark.integration]

_INSERT = (
    "INSERT INTO claims (owner, subject, verb, object, polarity, act, subject_canon, object_canon, "
    "status, valid_from, valid_to, fact_text) "
    "VALUES ($1, 'Nightingale', 'uses', $2, 'positive', 'assert', 'nightingale', $3, $4, now(), $5, $6) "
    "RETURNING claim_id")


async def _claim(conn, obj, *, owner="alice", status="grounded", closed=False, text="set"):
    return int(await conn.fetchval(
        _INSERT, owner, obj, obj.lower(), status, datetime(2026, 2, 1, tzinfo=timezone.utc) if closed else None,
        f"Nightingale uses {obj}" if text == "set" else text))


def _vec(x: float) -> list[float]:
    return [x] * 768


async def _row(conn, cid):
    return await conn.fetchrow(
        "SELECT fact_vec IS NOT NULL AS has_vec, embed_model, valid, "
        "vector_dims(fact_vec) AS dims FROM claims WHERE claim_id = $1", cid)


@pytest.mark.asyncio
async def test_missing_vectors_returns_only_current_grounded_claims(db_pool, clean_hermes_test_db):
    async with db_pool.acquire() as conn:
        current = await _claim(conn, "Atlas")
        await _claim(conn, "Borealis", closed=True)
        await _claim(conn, "Cirrus", status="unconfirmed")
        await _claim(conn, "Delta", text=None)
        got = await claims_missing_vectors(conn, "alice", [])
        assert got == []
        all_ids = [int(r["claim_id"]) for r in await conn.fetch("SELECT claim_id FROM claims")]
        assert await claims_missing_vectors(conn, "alice", all_ids) == [(current, "Nightingale uses Atlas")]


@pytest.mark.asyncio
async def test_missing_vectors_is_owner_scoped_and_skips_filled(db_pool, clean_hermes_test_db):
    async with db_pool.acquire() as conn:
        mine = await _claim(conn, "Atlas")
        theirs = await _claim(conn, "Atlas", owner="bob")
        assert await claims_missing_vectors(conn, "alice", [mine, theirs]) == [(mine, "Nightingale uses Atlas")]
        await write_fact_vectors(conn, [(mine, _vec(0.1))], "nomic-embed-text")
        assert await claims_missing_vectors(conn, "alice", [mine]) == []


@pytest.mark.asyncio
async def test_write_stores_768d_vector_and_stamps_model_only_on_current(db_pool, clean_hermes_test_db):
    async with db_pool.acquire() as conn:
        current = await _claim(conn, "Atlas")
        old = await _claim(conn, "Borealis", closed=True)
        before = await _row(conn, current)
        assert await write_fact_vectors(conn, [(current, _vec(0.25)), (old, _vec(0.5))],
                                        "nomic-embed-text") == 1
        row = await _row(conn, current)
        assert (row["has_vec"], row["embed_model"], row["dims"]) == (True, "nomic-embed-text", 768)
        assert row["valid"] == before["valid"]  # the legacy valid column is never written
        stale = await _row(conn, old)
        assert (stale["has_vec"], stale["embed_model"]) == (False, None)


@pytest.mark.asyncio
async def test_write_refuses_wrong_dimension_and_writes_nothing(db_pool, clean_hermes_test_db):
    async with db_pool.acquire() as conn:
        good = await _claim(conn, "Atlas")
        bad = await _claim(conn, "Borealis")
        with pytest.raises(ValueError):
            await write_fact_vectors(conn, [(good, _vec(0.1)), (bad, [0.1] * 767)], "nomic-embed-text")
        for cid in (good, bad):
            assert (await _row(conn, cid))["has_vec"] is False
