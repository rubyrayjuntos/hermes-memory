"""Rules publish is fence-checked and idempotent (spec §5.1 publish, §5.2 steps 1-3)."""
from __future__ import annotations

import pytest

from hermes_memory.extract_publish import publish_rules
from hermes_memory.extraction_jobs import job_specs

from .claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]

SENTENCE = "Acme uses Postgres for the ledger service."


async def _set_content(db_pool, span_id: int, content: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE conversations SET content = $2 WHERE id = $1", span_id, content)


async def _claimed_rules_job(store, db_pool, owner="alice", text=SENTENCE):
    span_id = await span(store, owner, 1)
    await _set_content(db_pool, span_id, text)
    kind, op_id, payload = job_specs(span_id)[0]
    assert kind == "extract_rules"
    await store.job_add(owner, kind, op_id, payload)
    [(job_id, fence, _)] = await store.job_claim(kind, "w1", 1, lease_s=60)
    return span_id, job_id, fence


async def _embed(texts):
    return [[0.01] * 768 for _ in texts]


async def _evidence_count(db_pool, span_id: int) -> int:
    async with db_pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM claim_evidence WHERE span_id = $1", span_id))


async def _publish(db_pool, embed, job_id, fence):
    async with db_pool.acquire() as conn:
        return await publish_rules(conn, embed, job_id, fence)


@pytest.mark.asyncio
async def test_publish_writes_one_grounded_claim_with_a_768d_vector(store, db_pool, clean_hermes_test_db):
    span_id, job_id, fence = await _claimed_rules_job(store, db_pool)
    ids = await _publish(db_pool, _embed, job_id, fence)
    assert len(ids) == 1
    assert await current(db_pool, "alice", "acme", "uses") == [("Postgres for the ledger service", "positive")]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT vector_dims(fact_vec) AS dims, embed_model FROM claims WHERE claim_id = $1", ids[0])
        status = await conn.fetchval("SELECT status FROM durable_job WHERE id = $1", job_id)
    assert (row["dims"], row["embed_model"]) == (768, "nomic-embed-text")
    assert status == "done"
    assert await _evidence_count(db_pool, span_id) == 1


@pytest.mark.asyncio
async def test_second_publish_adds_no_evidence(store, db_pool, clean_hermes_test_db):
    span_id, job_id, fence = await _claimed_rules_job(store, db_pool)
    first = await _publish(db_pool, _embed, job_id, fence)
    async with db_pool.acquire() as conn:  # a redelivered job: same fence, lease still held
        await conn.execute("UPDATE durable_job SET status = 'leased' WHERE id = $1", job_id)
    second = await _publish(db_pool, _embed, job_id, fence)
    assert second == first
    assert await _evidence_count(db_pool, span_id) == 1
    assert len(await current(db_pool, "alice", "acme", "uses")) == 1


@pytest.mark.asyncio
async def test_old_fence_writes_nothing_after_done(store, db_pool, clean_hermes_test_db):
    span_id, job_id, fence = await _claimed_rules_job(store, db_pool)
    assert await store.job_done(job_id, fence) is True
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(list(texts))
        return await _embed(texts)

    assert await _publish(db_pool, embed, job_id, fence) == []
    assert await _evidence_count(db_pool, span_id) == 0
    assert await current(db_pool, "alice", "acme", "uses") == []
    assert calls == []


@pytest.mark.asyncio
async def test_stale_fence_after_reclaim_writes_nothing(store, db_pool, clean_hermes_test_db):
    span_id, job_id, fence = await _claimed_rules_job(store, db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
                           job_id)
    [(_, fence2, _)] = await store.job_claim("extract_rules", "w2", 1, lease_s=60)
    assert fence2 == fence + 1
    assert await _publish(db_pool, _embed, job_id, fence) == []
    assert await _evidence_count(db_pool, span_id) == 0
    assert len(await _publish(db_pool, _embed, job_id, fence2)) == 1


@pytest.mark.asyncio
async def test_none_vectors_are_skipped_and_job_still_done(store, db_pool, clean_hermes_test_db):
    _, job_id, fence = await _claimed_rules_job(store, db_pool)

    async def embed(texts):
        return [None for _ in texts]

    ids = await _publish(db_pool, embed, job_id, fence)
    assert len(ids) == 1
    async with db_pool.acquire() as conn:
        has_vec = await conn.fetchval("SELECT fact_vec IS NOT NULL FROM claims WHERE claim_id = $1", ids[0])
        status = await conn.fetchval("SELECT status FROM durable_job WHERE id = $1", job_id)
    assert has_vec is False and status == "done"
