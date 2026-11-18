"""Fence-checked rules publish (spec §5.1 publish, §5.2 steps 1-3).

One transaction: confirm the lease, place rules evidence, fill fact vectors for
the claims touched, settle the job. A stale fence rolls everything back.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable, Sequence

from .reception import extract_assertions, extract_self_assertions
from .store_claims import (
    RULES_EXTRACTOR,
    RULES_VERSION,
    _claim_lock_identity,
    _write_turn_entities,
    add_evidence,
    claims_missing_vectors,
    lock_timeline,
    rebuild_keys,
    record_rule_claims_on_conn,
    span_info,
    write_fact_vectors,
)
from .store_jobs import job_fence_check
from .turn_filter import segment_turn
from .versions import MODEL_VERSION

FACT_EMBED_MODEL = "nomic-embed-text"
MODEL_EXTRACTOR = "model"

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
            content = str(turn["content"])
            for seg in segment_turn(content):
                if seg.kind == "code":
                    continue
                piece = content[seg.start:seg.end]
                claims = extract_assertions(piece)
                if seg.kind == "authored":
                    # SPEC2: extract_self_assertions after extract_assertions (authored only)
                    claims.extend(extract_self_assertions(piece))
                if not claims:
                    continue
                if seg.kind == "authored":
                    ids = await record_rule_claims_on_conn(
                        conn, span_id, claims, RULES_EXTRACTOR, RULES_VERSION,
                        seg_start=seg.start, seg_end=seg.end)
                else:
                    ids = await record_rule_claims_on_conn(
                        conn, span_id, claims, "rules_document", RULES_VERSION,
                        speaker="document", seg_start=seg.start, seg_end=seg.end)
                claim_ids.extend(ids)
            claim_ids = list(dict.fromkeys(claim_ids))
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


async def publish_model(conn, store_embed: StoreEmbed, claims: list[dict], job_id: int, fence: int) -> list[int]:
    """Publish grounded model claims for one leased `extract_model` job; returns touched claim ids.

    Returns [] and writes nothing when the fence no longer matches. Model
    evidence supersedes only the live rules evidence of the same span,
    timeline key, polarity and object; other rules evidence stays live.
    Distinct timeline locks are taken in sorted (owner, subject_id, verb) order
    before any evidence is written. Current claim text is embedded in this
    transaction (same as publish_rules). This function does not settle the job.
    """
    async with conn.transaction():
        if not await job_fence_check(conn, job_id, fence):
            return []
        job = await conn.fetchrow("SELECT owner, payload FROM durable_job WHERE id = $1", int(job_id))
        raw = job["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        span_ids = [int(value) for value in payload.get("span_ids", [payload.get("span_id")])
                    if value is not None]
        infos = {}
        for span_id in span_ids:
            info = await span_info(conn, span_id)
            if info.owner == job["owner"]:
                infos[span_id] = info

        locks = set()
        for c in claims:
            span_id = c.get("span_id")
            if span_id is None and len(infos) == 1:
                span_id = next(iter(infos))
            info = infos.get(int(span_id)) if span_id is not None else None
            if info is None:
                continue
            ident = await _claim_lock_identity(conn, info, c)
            if ident is not None:
                locks.add(ident)
        for owner, subject_id, verb in sorted(locks):
            await lock_timeline(conn, owner, subject_id, verb)

        kept_by_span: dict[int, list[tuple[int, int]]] = {}
        for claim in claims:
            span_id = claim.get("span_id")
            if span_id is None and len(infos) == 1:
                span_id = next(iter(infos))
            if span_id is None or int(span_id) not in infos:
                continue
            resolved_span_id = int(span_id)
            added = await add_evidence(
                conn, infos[resolved_span_id], resolved_span_id, claim,
                extractor=MODEL_EXTRACTOR, version=MODEL_VERSION,
            )
            if added is not None:
                kept_by_span.setdefault(resolved_span_id, []).append(added)
        kept = [item for items in kept_by_span.values() for item in items]
        if not kept:
            return []
        ev_ids = list(dict.fromkeys(e for e, _ in kept))
        await conn.execute(
            "UPDATE claim_evidence r SET state = 'superseded_version' FROM claim_evidence m "
            "WHERE m.id = ANY($1::bigint[]) AND r.owner = m.owner AND r.span_id = m.span_id "
            "AND r.subject_id = m.subject_id AND r.verb = m.verb AND r.object_key = m.object_key "
            "AND r.polarity = m.polarity AND r.act = m.act "
            "AND r.extractor = $2 AND r.extractor_version = $3 AND r.state = 'live'",
            ev_ids, RULES_EXTRACTOR, RULES_VERSION)
        await rebuild_keys(conn, [k for _, k in kept])
        for span_id, items in kept_by_span.items():
            await _write_turn_entities(
                conn, infos[span_id].owner, span_id,
                list(dict.fromkeys(evidence_id for evidence_id, _ in items)),
            )
        owner_of = {int(r["id"]): int(r["claim_id"]) for r in await conn.fetch(
            "SELECT id, claim_id FROM claim_evidence WHERE id = ANY($1::bigint[])", ev_ids)}
        claim_ids = list(dict.fromkeys(owner_of[e] for e in ev_ids))
        missing = await claims_missing_vectors(conn, job["owner"], claim_ids)
        if missing:
            vectors = await store_embed([text for _, text in missing])
            if len(vectors) != len(missing):
                raise ValueError(f"embedder returned {len(vectors)} vectors for {len(missing)} texts")
            rows = [(cid, vec) for (cid, _), vec in zip(missing, vectors) if vec is not None]
            if rows:
                await write_fact_vectors(conn, rows, FACT_EMBED_MODEL)
        return claim_ids
