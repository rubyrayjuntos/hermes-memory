"""Fence-checked rules publish (spec §5.1 publish, §5.2 steps 1-3).

One transaction: confirm the lease, place rules evidence, fill fact vectors for
the claims touched, settle the job. A stale fence rolls everything back.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable, Sequence

from .reception import extract_assertions
from .store_claims import (
    RULES_EXTRACTOR,
    RULES_VERSION,
    claims_missing_vectors,
    record_rule_claims_on_conn,
    write_fact_vectors,
)
from .store_jobs import job_fence_check

FACT_EMBED_MODEL = "nomic-embed-text"

StoreEmbed = Callable[[Sequence[str]], Awaitable[list]]


async def publish_rules(conn, store_embed: StoreEmbed, job_id: int, fence: int) -> list[int]:
    """Publish rules-tier claims for one leased `extract_rules` job; returns touched claim ids.

    Returns [] and writes nothing when the fence no longer matches. Embedding
    failures raise and roll the transaction back, leaving the job leased.
    """
    async with conn.transaction():
        if not await job_fence_check(conn, job_id, fence):
            return []
        job = await conn.fetchrow("SELECT owner, payload FROM durable_job WHERE id = $1", int(job_id))
        raw = job["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        span_id = int(payload["span_id"])
        turn = await conn.fetchrow(
            "SELECT role, content FROM conversations WHERE id = $1 AND agent_identity = $2",
            span_id, job["owner"])
        claim_ids: list[int] = []
        if turn is not None and turn["role"] == "user" and (turn["content"] or "").strip():
            claims = extract_assertions(turn["content"])
            if claims:
                claim_ids = list(dict.fromkeys(await record_rule_claims_on_conn(
                    conn, span_id, claims, RULES_EXTRACTOR, RULES_VERSION)))
        missing = await claims_missing_vectors(conn, job["owner"], claim_ids)
        if missing:
            vectors = await store_embed([text for _, text in missing])
            if len(vectors) != len(missing):
                raise ValueError(f"embedder returned {len(vectors)} vectors for {len(missing)} texts")
            rows = [(cid, vec) for (cid, _), vec in zip(missing, vectors) if vec is not None]
            if rows:
                await write_fact_vectors(conn, rows, FACT_EMBED_MODEL)
        await conn.execute(
            "UPDATE durable_job SET status = 'done', claimed_by = NULL, lease_expires_at = NULL "
            "WHERE id = $1 AND fence = $2 AND status = 'leased'", int(job_id), int(fence))
        return claim_ids
