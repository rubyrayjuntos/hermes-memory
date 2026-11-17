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
async def test_model_batch_waits_for_threshold_or_flush_and_tracks_usage(store, clean_hermes_test_db):
    for span_id in range(1, 8):
        await store.job_add_model_batch("alice", "batch-session", span_id)
    assert await store.job_claim(
        "extract_model", "w", 1, lease_s=120, session_ordered=True,
        ready_model_batch=True,
    ) == []

    await store.job_add("alice", "extract_rules", "rule-next", {"span_id": 8},
                        session_id="batch-session")
    assert len(await store.job_claim("extract_rules", "w", 1, lease_s=60,
                                    session_ordered=True)) == 1

    await store.job_flush_model_batches("alice", "batch-session")
    [(job_id, fence, payload)] = await store.job_claim(
        "extract_model", "w", 1, lease_s=120, session_ordered=True,
        ready_model_batch=True,
    )
    assert payload["span_ids"] == list(range(1, 8))
    assert await store.job_record_model_call(job_id, fence)
    assert await store.job_record_model_tokens(job_id, fence, 70, 20)
    assert await store.job_done(job_id, fence)
    usage = await store.model_extraction_usage()
    assert usage["batches_completed"] == 1
    assert usage["calls"] == 1
    assert usage["prompt_tokens"] == 70
    assert usage["completion_tokens"] == 20
    assert usage["calls_per_completed_batch"] == 1
    assert usage["total_tokens"] == 90


@pytest.mark.asyncio
async def test_model_batch_claims_at_eight_eligible_turns(store, clean_hermes_test_db):
    for span_id in range(1, 9):
        await store.job_add_model_batch("alice", "batch-session", span_id)
    [(job_id, fence, payload)] = await store.job_claim(
        "extract_model", "w", 1, lease_s=120, session_ordered=True,
        ready_model_batch=True,
    )
    assert payload["span_ids"] == list(range(1, 9))
    assert await store.job_done(job_id, fence)


@pytest.mark.asyncio
async def test_model_batch_claims_after_max_wait(store, db_pool, clean_hermes_test_db):
    await store.job_add_model_batch("alice", "batch-session", 1)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE durable_job SET payload = jsonb_set(payload, '{batch_started_at}', "
            "to_jsonb((now() - interval '31 minutes')::text), true) "
            "WHERE owner = 'alice' AND session_id = 'batch-session' AND kind = 'extract_model'"
        )
    [(job_id, fence, payload)] = await store.job_claim(
        "extract_model", "w", 1, lease_s=120, session_ordered=True,
        ready_model_batch=True,
    )
    assert payload["span_ids"] == [1]
    assert await store.job_done(job_id, fence)


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


@pytest.mark.asyncio
async def test_defer_backfill_holds_priority_jobs_while_a_live_job_is_open(store, clean_hermes_test_db):
    await store.job_add("alice", "extract_rules", "op-live", {"n": "live"}, priority=0)
    await store.job_add("alice", "extract_rules", "op-backfill", {"n": "backfill"}, priority=10)
    claimed = await store.job_claim("extract_rules", "w", 10, lease_s=60, defer_backfill=True)
    assert [p["n"] for _, _, p in claimed] == ["live"]
    [(live_id, live_fence, _)] = claimed
    # The live job is still leased, so backfill stays held.
    assert await store.job_claim("extract_rules", "w", 10, lease_s=60, defer_backfill=True) == []
    assert await store.job_done(live_id, live_fence) is True
    second = await store.job_claim("extract_rules", "w", 10, lease_s=60, defer_backfill=True)
    assert [p["n"] for _, _, p in second] == ["backfill"]


@pytest.mark.asyncio
async def test_defer_backfill_off_claims_both(store, clean_hermes_test_db):
    await store.job_add("alice", "extract_rules", "op-live", {}, priority=0)
    await store.job_add("alice", "extract_rules", "op-backfill", {}, priority=10)
    assert len(await store.job_claim("extract_rules", "w", 10, lease_s=60)) == 2
