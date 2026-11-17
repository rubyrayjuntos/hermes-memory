"""Model publish supersedes only the rules evidence it matches (spec §5.2 steps 3-4)."""
from __future__ import annotations

import pytest

from hermes_memory.extract_publish import publish_model

from .claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]

POSTGRES = {"subject": "Acme", "verb": "uses", "object": "Postgres", "polarity": "positive"}
REDIS = {"subject": "Acme", "verb": "uses", "object": "Redis", "polarity": "positive"}


def _vec(seed: float = 0.1) -> list[float]:
    return [seed + (i * 0.0001) for i in range(768)]


async def _embed(texts):
    return [_vec(0.2 + i * 0.01) for i, _ in enumerate(texts)]


async def _model_job(store, span_id: int, owner: str = "alice"):
    await store.job_add(
        owner,
        "extract_model",
        f"s:window:{span_id}:1",
        {"span_ids": [span_id], "extractor": "model", "version": "1", "usage": {"calls": 0}},
        session_id="s",
    )
    [(job_id, fence, _)] = await store.job_claim("extract_model", "w1", 1, lease_s=60)
    return job_id, fence


async def _publish(db_pool, claims, job_id, fence, embed=_embed):
    async with db_pool.acquire() as conn:
        return await publish_model(conn, embed, claims, job_id, fence)


async def _states(db_pool, span_id: int) -> list[tuple[str, str, str]]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT extractor, object_surface, state FROM claim_evidence WHERE span_id = $1 "
            "ORDER BY extractor, object_surface, id", span_id)
    return [(r["extractor"], r["object_surface"], r["state"]) for r in rows]


@pytest.mark.asyncio
async def test_matching_model_claim_supersedes_the_rules_evidence(store, db_pool, clean_hermes_test_db):
    span_id = await span(store, "alice", 1)
    await store.record_rule_claims(span_id, [POSTGRES])
    job_id, fence = await _model_job(store, span_id)
    ids = await _publish(db_pool, [POSTGRES], job_id, fence)
    assert len(ids) == 1
    assert await _states(db_pool, span_id) == [
        ("model", "Postgres", "live"), ("rules", "Postgres", "superseded_version")]
    assert await current(db_pool, "alice", "acme", "uses") == [("Postgres", "positive")]
    async with db_pool.acquire() as conn:
        versions = await conn.fetch(
            "SELECT extractor_version FROM claim_evidence WHERE extractor = 'model' AND span_id = $1", span_id)
    assert [r["extractor_version"] for r in versions] == ["1"]


@pytest.mark.asyncio
async def test_rules_claim_with_a_different_object_stays_live(store, db_pool, clean_hermes_test_db):
    span_id = await span(store, "alice", 1)
    await store.record_rule_claims(span_id, [POSTGRES, REDIS])
    job_id, fence = await _model_job(store, span_id)
    await _publish(db_pool, [POSTGRES], job_id, fence)
    assert await _states(db_pool, span_id) == [
        ("model", "Postgres", "live"),
        ("rules", "Postgres", "superseded_version"),
        ("rules", "Redis", "live"),
    ]


@pytest.mark.asyncio
async def test_publishing_twice_adds_no_evidence(store, db_pool, clean_hermes_test_db):
    span_id = await span(store, "alice", 1)
    await store.record_rule_claims(span_id, [POSTGRES])
    job_id, fence = await _model_job(store, span_id)
    first = await _publish(db_pool, [POSTGRES], job_id, fence)
    second = await _publish(db_pool, [POSTGRES], job_id, fence)
    assert second == first
    assert len(await _states(db_pool, span_id)) == 2


@pytest.mark.asyncio
async def test_stale_fence_changes_nothing(store, db_pool, clean_hermes_test_db):
    span_id = await span(store, "alice", 1)
    await store.record_rule_claims(span_id, [POSTGRES])
    job_id, fence = await _model_job(store, span_id)
    assert await store.job_done(job_id, fence) is True
    before = await _states(db_pool, span_id)
    assert await _publish(db_pool, [POSTGRES], job_id, fence) == []
    assert await _states(db_pool, span_id) == before == [("rules", "Postgres", "live")]


@pytest.mark.asyncio
async def test_model_only_claim_gets_fact_vec(store, db_pool, clean_hermes_test_db):
    span_id = await span(store, "alice", 1)
    job_id, fence = await _model_job(store, span_id)
    ids = await _publish(db_pool, [POSTGRES], job_id, fence)
    assert len(ids) == 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT vector_dims(fact_vec) AS dims, embed_model, fact_text "
            "FROM claims WHERE claim_id = $1", ids[0])
    assert row["fact_text"]
    assert (row["dims"], row["embed_model"]) == (768, "nomic-embed-text")
