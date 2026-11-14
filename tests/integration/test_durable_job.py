"""durable_job lifecycle (spec §5.1)."""
from __future__ import annotations

import asyncio

import pytest

from hermes_memory.store_jobs import job_fence_check

pytestmark = [pytest.mark.store, pytest.mark.integration]


async def _expire(db_pool, job_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET lease_expires_at = now() - interval '1 second' WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_two_claimers_never_get_the_same_job(store, clean_hermes_test_db):
    for i in range(8):
        await store.job_add("alice", "extract_rules", f"op-{i}", {"span_id": i})
    a, b = await asyncio.gather(store.job_claim("extract_rules", "w1", 5, lease_s=60),
                                store.job_claim("extract_rules", "w2", 5, lease_s=60))
    ids_a, ids_b = {j for j, _, _ in a}, {j for j, _, _ in b}
    assert ids_a.isdisjoint(ids_b) and len(ids_a | ids_b) == 8


@pytest.mark.asyncio
async def test_session_order_blocks_later_jobs_of_the_same_kind_only(store, clean_hermes_test_db):
    await store.job_add("alice", "extract_rules", "op-1", {}, session_id="s", event_time=None)
    async with store.pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET event_time = now() - interval '2 minutes' WHERE op_id = 'op-1'")
    await store.job_add("alice", "extract_rules", "op-2", {}, session_id="s")
    await store.job_add("alice", "extract_model", "op-3", {}, session_id="s")
    async with store.pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET event_time = now() WHERE op_id IN ('op-2', 'op-3')")
    [(first, _, _)] = await store.job_claim("extract_rules", "w", 10, lease_s=60, session_ordered=True)
    assert await store.job_claim("extract_rules", "w", 10, lease_s=60, session_ordered=True) == []
    assert len(await store.job_claim("extract_model", "w", 10, lease_s=60, session_ordered=True)) == 1


@pytest.mark.asyncio
async def test_stale_worker_cannot_publish_after_reclaim(store, db_pool, clean_hermes_test_db):
    await store.job_add("alice", "extract_rules", "op-x", {})
    [(jid, fence_a, _)] = await store.job_claim("extract_rules", "w1", 1, lease_s=60)
    await _expire(db_pool, jid)
    [(_, fence_b, _)] = await store.job_claim("extract_rules", "w2", 1, lease_s=60)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            assert await job_fence_check(conn, jid, fence_a) is False
            assert await job_fence_check(conn, jid, fence_b) is True
    assert await store.job_done(jid, fence_a) is False
    assert await store.job_heartbeat(jid, fence_a, 60) is False
    assert await store.job_done(jid, fence_b) is True


@pytest.mark.asyncio
async def test_retry_backs_off_then_fails_after_the_limit(store, db_pool, clean_hermes_test_db):
    await store.job_add("alice", "extract_model", "op-r", {})
    outcomes = []
    for _ in range(5):
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE durable_job SET next_attempt_at = now() - interval '1 second' WHERE op_id = 'op-r'")
        [(jid, fence, _)] = await store.job_claim("extract_model", "w", 1, lease_s=60)
        outcomes.append(await store.job_retry(jid, fence, "boom"))
    assert outcomes == ["retryable"] * 4 + ["failed"]
    assert (await store.job_counts())["extract_model"]["failed"] == 1


@pytest.mark.asyncio
async def test_v19_copies_pending_outbox_rows_and_is_idempotent(db_pool, clean_hermes_test_db):
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "sql" / "migrations" / "V19__durable_job.sql").read_text()
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM write_outbox")
        await conn.execute("INSERT INTO write_outbox (agent_identity, item, op_id, attempts) "
                           "VALUES ('alice', '{\"type\": \"turn\"}', 'op-legacy', 1), "
                           "('alice', '{\"type\": \"turn\"}', 'op-poison', 5)")
        await conn.execute(sql)
        await conn.execute(sql)
        rows = {r["op_id"]: r["status"] for r in await conn.fetch(
            "SELECT op_id, status FROM durable_job WHERE kind = 'capture' AND owner = 'alice'")}
        assert rows == {"op-legacy": "pending", "op-poison": "failed"}
        assert await conn.fetchval("SELECT count(*) FROM write_outbox") == 0
