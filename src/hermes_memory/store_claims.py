# src/hermes_memory/store_claims.py
"""Owner-scoped entities, claim evidence and timelines (graph layer spec §4, §5.3).

Module functions take an asyncpg connection so one transaction can compose
them (reception, repair, merges, deletion, the legacy data migration).
StoreClaimsMixin wraps them for callers that hold a pool.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

import asyncpg

from .entities import (
    SELF_NAME,
    SELF_NORM,
    classify_object,
    is_abstained,
    is_first_person,
    normalize_name,
)
from .timeline import Event, rebuild

logger = logging.getLogger("hermes_memory.store_claims")


@dataclass(frozen=True)
class SpanInfo:
    owner: str
    ts: datetime
    role: str
    session_id: str


async def span_info(conn, span_id: int) -> SpanInfo:
    row = await conn.fetchrow(
        "SELECT agent_identity, ts, role, session_id FROM conversations WHERE id = $1", int(span_id))
    if row is None:
        raise LookupError(f"span {span_id} not found")
    return SpanInfo(row["agent_identity"], row["ts"], row["role"], row["session_id"])


async def follow_merges(conn, owner: str, entity_id: int) -> int:
    cur = int(entity_id)
    for _ in range(16):
        nxt = await conn.fetchval("SELECT merged_into FROM entity WHERE owner = $1 AND id = $2", owner, cur)
        if nxt is None:
            return cur
        cur = int(nxt)
    logger.warning("merge chain longer than 16 for owner=%s entity=%s", owner, entity_id)
    return cur


async def resolve_entity(conn, owner: str, surface: str) -> Optional[int]:
    if is_first_person(surface):
        sid = await conn.fetchval("SELECT id FROM entity WHERE owner = $1 AND type = 'self'", owner)
        return int(sid) if sid is not None else None
    if is_abstained(surface):
        return None
    norm = normalize_name(surface)
    eid = await conn.fetchval(
        "SELECT entity_id FROM entity_alias WHERE owner = $1 AND surface_norm = $2 AND state = 'live' "
        "ORDER BY created_at, entity_id LIMIT 1", owner, norm)
    if eid is None:
        eid = await conn.fetchval(
            "SELECT id FROM entity WHERE owner = $1 AND name_norm = $2 ORDER BY id LIMIT 1", owner, norm)
    return None if eid is None else await follow_merges(conn, owner, int(eid))


async def ensure_entity(conn, owner: str, surface: str, type_: str = "other") -> int:
    found = await resolve_entity(conn, owner, surface)
    if found is not None:
        return found
    if is_first_person(surface):
        name, norm, type_ = SELF_NAME, SELF_NORM, "self"
    elif is_abstained(surface):
        raise ValueError(f"abstained surface cannot name an entity: {surface!r}")
    else:
        name, norm = surface.strip(), normalize_name(surface)
    new_id = await conn.fetchval(
        "INSERT INTO entity (owner, name, name_norm, type) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT DO NOTHING RETURNING id", owner, name, norm, type_)
    if new_id is not None:
        return int(new_id)
    if type_ == "self":
        got = await conn.fetchval("SELECT id FROM entity WHERE owner = $1 AND type = 'self'", owner)
    else:
        got = await conn.fetchval(
            "SELECT id FROM entity WHERE owner = $1 AND name_norm = $2 AND type = $3", owner, norm, type_)
    return await follow_merges(conn, owner, int(got))


RULES_EXTRACTOR = "rules"
DOCUMENT_EXTRACTOR = "rules_document"
RULES_VERSION = "2"
OBJECT_KEY_SQL = "(CASE WHEN object_id IS NOT NULL THEN 'e:' || object_id::text ELSE 'l:' || object_canon END)"
_SPEAKERS = {"user": "user", "assistant": "assistant"}
_ALLOWED_SPEAKERS = frozenset({"user", "assistant", "document"})


@dataclass(frozen=True)
class TimelineKey:
    owner: str
    subject_id: int
    verb: str
    object_key: Optional[str]


def sort_key(k: TimelineKey) -> tuple:
    return (k.owner, k.subject_id, k.verb, k.object_key or "")


async def lock_timeline(conn, owner: str, subject_id: int, verb: str) -> None:
    """Serialize every writer of one (owner, subject, verb) for this transaction.

    Taken before placing evidence as well as before the rebuild, so no writer
    can point evidence at a placeholder another writer is about to delete.
    Re-entrant within a transaction.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                       f"timeline|{owner}|{int(subject_id)}|{verb}")


async def verb_row(conn, verb_or_phrase: str):
    raw = (verb_or_phrase or "").strip().lower()
    row = await conn.fetchrow("SELECT verb, functional, phrase, neg_phrase FROM verb WHERE verb = $1", raw)
    if row is None:
        row = await conn.fetchrow(
            "SELECT v.verb, v.functional, v.phrase, v.neg_phrase FROM verb_synonym s "
            "JOIN verb v ON v.verb = s.verb WHERE s.phrase_norm = $1", normalize_name(raw))
    return row


def timeline_key(owner: str, subject_id: int, verb_rec, object_key: str) -> TimelineKey:
    return TimelineKey(owner, int(subject_id), verb_rec["verb"], None if verb_rec["functional"] else object_key)


async def object_key_for(conn, owner: str, surface: str) -> Optional[str]:
    kind = classify_object(surface)
    if kind == "abstain":
        return None
    if kind == "entity":
        return f"e:{await ensure_entity(conn, owner, surface)}"
    canon = normalize_name(surface)
    return f"l:{canon}" if canon else None


async def _object_fields(conn, owner: str, object_key: str, surfaces: Sequence[str]) -> tuple:
    if object_key.startswith("e:"):
        oid = int(object_key[2:])
        ent = await conn.fetchrow("SELECT name, name_norm FROM entity WHERE owner = $1 AND id = $2", owner, oid)
        return oid, ent["name_norm"], ent["name"]
    canon = object_key[2:]
    text = next((s for s in reversed(surfaces) if s), canon)
    return None, canon, text


_INSERT_CLAIM = """
INSERT INTO claims (owner, span_id, subject, verb, object, polarity, act, uptake,
                    subject_canon, object_canon, subject_id, object_id, status,
                    valid_from, valid_to, fact_text)
VALUES ($1, $2, $3, $4, $5, $6, 'assert', 'unknown', $7, $8, $9, $10, $11, $12, $13, $14)
RETURNING claim_id
"""
_UPDATE_CLAIM = """
UPDATE claims SET owner = $2, span_id = $3, subject = $4, verb = $5, object = $6, polarity = $7,
                  subject_canon = $8, object_canon = $9, subject_id = $10, object_id = $11,
                  status = $12, valid_from = $13, valid_to = $14, fact_text = $15
 WHERE claim_id = $1
"""


async def placeholder_claim(conn, owner: str, subject_id: int, verb: str, object_key: str,
                            polarity: str, span_id: int) -> int:
    """A row for new evidence to point at; the rebuild assigns real rows."""
    found = await conn.fetchval(
        f"SELECT claim_id FROM claims WHERE owner = $1 AND subject_id = $2 AND verb = $3 "
        f"AND {OBJECT_KEY_SQL} = $4 AND polarity = $5 "
        f"ORDER BY (status = 'grounded' AND valid_to IS NULL) DESC, claim_id LIMIT 1",
        owner, subject_id, verb, object_key, polarity)
    if found is not None:
        return int(found)
    subj = await conn.fetchrow("SELECT name, name_norm FROM entity WHERE owner = $1 AND id = $2", owner, subject_id)
    oid, canon, text = await _object_fields(conn, owner, object_key, [])
    return int(await conn.fetchval(
        _INSERT_CLAIM, owner, span_id, subj["name"], verb, text, polarity, subj["name_norm"], canon,
        subject_id, oid, "orphaned", None, None, None))


async def _validated_seg_offsets(conn, span_id: int, seg_start, seg_end):
    if seg_start is None and seg_end is None:
        return None, None
    if seg_start is None or seg_end is None:
        return None
    length = await conn.fetchval(
        "SELECT char_length(content) FROM conversations WHERE id = $1", int(span_id))
    start, end = int(seg_start), int(seg_end)
    if 0 <= start <= end <= int(length or 0):
        return start, end
    return None


async def add_evidence(conn, info: SpanInfo, span_id: int, claim: dict, *, act: str = "assert",
                       extractor: str = RULES_EXTRACTOR, version: str = RULES_VERSION,
                       speaker: str | None = None, seg_start: int | None = None,
                       seg_end: int | None = None):
    resolved = speaker or _SPEAKERS.get(info.role)
    subject, obj = str(claim.get("subject") or ""), str(claim.get("object") or "")
    if resolved not in _ALLOWED_SPEAKERS or is_abstained(subject):
        return None
    offsets = await _validated_seg_offsets(conn, span_id, seg_start, seg_end)
    if offsets is None:
        return None
    seg_start, seg_end = offsets
    vrec = await verb_row(conn, str(claim.get("verb") or ""))
    if vrec is None:
        logger.info("unknown verb skipped: %r", claim.get("verb"))
        return None
    object_key = await object_key_for(conn, info.owner, obj)
    if object_key is None:
        return None
    subject_id = await ensure_entity(conn, info.owner, subject)
    await lock_timeline(conn, info.owner, subject_id, vrec["verb"])
    polarity = claim.get("polarity") or "positive"
    holder = await placeholder_claim(conn, info.owner, subject_id, vrec["verb"], object_key, polarity, span_id)
    ev_id = await conn.fetchval(
        "INSERT INTO claim_evidence (owner, claim_id, span_id, speaker, act, polarity, subject_surface, "
        "object_surface, subject_id, verb, object_key, extractor, extractor_version, event_time, "
        "seg_start, seg_end) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) "
        "ON CONFLICT (owner, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act) "
        "WHERE state = 'live' DO NOTHING RETURNING id",
        info.owner, holder, int(span_id), resolved, act, polarity, subject, obj, subject_id, vrec["verb"],
        object_key, extractor, version, claim.get("event_time") or info.ts, seg_start, seg_end)
    if ev_id is None:
        ev_id = await conn.fetchval(
            "SELECT id FROM claim_evidence WHERE owner = $1 AND span_id = $2 AND extractor = $3 "
            "AND extractor_version = $4 AND subject_id = $5 AND verb = $6 AND object_key = $7 "
            "AND polarity = $8 AND act = $9 AND state = 'live'",
            info.owner, int(span_id), extractor, version, subject_id, vrec["verb"], object_key, polarity, act)
    return int(ev_id), timeline_key(info.owner, subject_id, vrec, object_key)


async def _claim_lock_identity(conn, info: SpanInfo, claim: dict) -> Optional[tuple]:
    """The (owner, subject_id, verb) `add_evidence` would lock for this claim, or None if it skips.

    Mirrors the skip rules of `add_evidence` so the batch can lock in sorted order first.
    """
    if _SPEAKERS.get(info.role) is None:
        return None
    subject = str(claim.get("subject") or "")
    if is_abstained(subject):
        return None
    vrec = await verb_row(conn, str(claim.get("verb") or ""))
    if vrec is None:
        return None
    if await object_key_for(conn, info.owner, str(claim.get("object") or "")) is None:
        return None
    subject_id = await ensure_entity(conn, info.owner, subject)
    return (info.owner, int(subject_id), vrec["verb"])


async def rebuild_key(conn, key: TimelineKey) -> None:
    vrec = await verb_row(conn, key.verb)
    await lock_timeline(conn, key.owner, key.subject_id, key.verb)
    args: list = [key.owner, key.subject_id, key.verb]
    ev_filter = row_filter = ""
    if key.object_key is not None:
        args.append(key.object_key)
        ev_filter, row_filter = " AND object_key = $4", f" AND {OBJECT_KEY_SQL} = $4"
    ev_rows = await conn.fetch(
        "SELECT id, claim_id, span_id, event_time, speaker, act, object_key, polarity, object_surface "
        "FROM claim_evidence WHERE owner = $1 AND subject_id = $2 AND verb = $3 AND state = 'live'" + ev_filter,
        *args)
    existing = [int(r["claim_id"]) for r in await conn.fetch(
        "SELECT claim_id FROM claims WHERE owner = $1 AND subject_id = $2 AND verb = $3" + row_filter, *args)]
    events = [Event(int(r["id"]), int(r["claim_id"]), int(r["span_id"]), r["event_time"], r["speaker"],
                    r["act"], r["object_key"], r["polarity"]) for r in ev_rows]
    plan = rebuild(events, existing, functional=bool(vrec["functional"]))
    surface = {int(r["id"]): r["object_surface"] for r in ev_rows}
    span_of = {int(r["id"]): int(r["span_id"]) for r in ev_rows}
    subj = await conn.fetchrow("SELECT name, name_norm FROM entity WHERE owner = $1 AND id = $2",
                               key.owner, key.subject_id)
    if existing:
        await conn.execute("UPDATE claims SET status = 'orphaned', superseded_by = NULL "
                           "WHERE claim_id = ANY($1::bigint[])", existing)
    ids: list[int] = []
    for row in plan.rows:
        oid, canon, text = await _object_fields(conn, key.owner, row.object_key,
                                                [surface[i] for i in row.evidence_ids])
        phrase = vrec["phrase"] if row.polarity == "positive" else vrec["neg_phrase"]
        values = (key.owner, max(span_of[i] for i in row.evidence_ids), subj["name"], key.verb, text,
                  row.polarity, subj["name_norm"], canon, key.subject_id, oid, row.status,
                  row.valid_from, row.valid_to, f"{subj['name']} {phrase} {text}")
        if row.claim_id is None:
            cid = int(await conn.fetchval(_INSERT_CLAIM, *values))
        else:
            cid = row.claim_id
            await conn.execute(_UPDATE_CLAIM, cid, *values)
        await conn.execute("UPDATE claim_evidence SET claim_id = $1 WHERE id = ANY($2::bigint[])",
                           cid, list(row.evidence_ids))
        ids.append(cid)
    for a, b in zip(plan.chain, plan.chain[1:]):
        await conn.execute("UPDATE claims SET superseded_by = $1 WHERE claim_id = $2", ids[b], ids[a])
    if plan.orphaned:
        await conn.execute(
            "DELETE FROM claims c WHERE c.claim_id = ANY($1::bigint[]) AND c.legacy_valid IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.owner = c.owner AND e.claim_id = c.claim_id) "
            "AND NOT EXISTS (SELECT 1 FROM claims s WHERE s.superseded_by = c.claim_id)",
            list(plan.orphaned))


async def rebuild_keys(conn, keys: Iterable[TimelineKey]) -> None:
    for key in sorted(set(keys), key=sort_key):
        await rebuild_key(conn, key)


async def _write_turn_entities(conn, owner: str, span_id: int, evidence_ids: Sequence[int]) -> None:
    rows = await conn.fetch(
        "SELECT subject_id, subject_surface, object_key, object_surface FROM claim_evidence "
        "WHERE id = ANY($1::bigint[])", list(evidence_ids))
    for r in rows:
        pairs = [(int(r["subject_id"]), r["subject_surface"])]
        if r["object_key"].startswith("e:"):
            pairs.append((int(r["object_key"][2:]), r["object_surface"]))
        for entity_id, surface in pairs:
            await conn.execute(
                "INSERT INTO turn_entity (owner, span_id, entity_id, surface) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT DO NOTHING", owner, int(span_id), entity_id, surface)


async def upsert_alias(conn, owner: str, surface_norm: str, entity_id: int, source: str,
                       span_id: Optional[int], event_time: Optional[datetime]) -> None:
    await conn.execute(
        "INSERT INTO entity_alias (owner, surface_norm, entity_id, source, span_id, event_time) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (owner, surface_norm, entity_id) DO UPDATE "
        "SET state = 'live', source = EXCLUDED.source, span_id = EXCLUDED.span_id, "
        "event_time = EXCLUDED.event_time",
        owner, surface_norm, int(entity_id), source, span_id, event_time)


async def reresolve(conn, owner: str, entity_ids: set[int]) -> set[TimelineKey]:
    """Re-resolve live evidence touching these entities from its stored surfaces.

    Resolution is computed first for every row; the distinct timeline locks of
    the rows that will move are then taken in sorted (owner, subject_id, verb)
    order, and only after that are placeholders and evidence updated.
    """
    keys: set[TimelineKey] = set()
    rows = await conn.fetch(
        "SELECT id, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act, "
        "subject_surface, object_surface FROM claim_evidence WHERE owner = $1 AND state = 'live' "
        "AND (subject_id = ANY($2::bigint[]) OR object_key = ANY($3::text[]))",
        owner, list(entity_ids), [f"e:{i}" for i in entity_ids])
    moves = []
    for r in rows:
        vrec = await verb_row(conn, r["verb"])
        keys.add(timeline_key(owner, int(r["subject_id"]), vrec, r["object_key"]))
        new_sub = await resolve_entity(conn, owner, r["subject_surface"]) or int(r["subject_id"])
        new_obj = r["object_key"]
        if new_obj.startswith("e:"):
            o = await resolve_entity(conn, owner, r["object_surface"])
            if o is not None:
                new_obj = f"e:{o}"
        if (new_sub, new_obj) == (int(r["subject_id"]), r["object_key"]):
            continue
        moves.append((r, vrec, new_sub, new_obj))
    # A move takes evidence off the old subject's timeline and puts it on the new one,
    # so both are locked here, in one sorted acquisition, before any update.
    lock_set = set()
    for r, _, new_sub, _ in moves:
        lock_set.add((owner, int(r["subject_id"]), r["verb"]))
        lock_set.add((owner, new_sub, r["verb"]))
    for lock_owner, subject_id, verb in sorted(lock_set):
        await lock_timeline(conn, lock_owner, subject_id, verb)
    for r, vrec, new_sub, new_obj in moves:
        clash = await conn.fetchval(
            "SELECT id FROM claim_evidence WHERE owner = $1 AND span_id = $2 AND extractor = $3 "
            "AND extractor_version = $4 AND subject_id = $5 AND verb = $6 AND object_key = $7 "
            "AND polarity = $8 AND act = $9 AND state = 'live' AND id <> $10",
            owner, r["span_id"], r["extractor"], r["extractor_version"], new_sub, r["verb"], new_obj,
            r["polarity"], r["act"], r["id"])
        if clash is not None:
            await conn.execute("UPDATE claim_evidence SET state = 'superseded_version' WHERE id = $1", r["id"])
        else:
            await lock_timeline(conn, owner, new_sub, r["verb"])
            holder = await placeholder_claim(conn, owner, new_sub, r["verb"], new_obj, r["polarity"], r["span_id"])
            await conn.execute(
                "UPDATE claim_evidence SET subject_id = $2, object_key = $3, claim_id = $4 WHERE id = $1",
                r["id"], new_sub, new_obj, holder)
        keys.add(timeline_key(owner, new_sub, vrec, new_obj))
    return keys


async def unmerge_unjustified(conn, owner: str) -> set[TimelineKey]:
    rows = await conn.fetch(
        "SELECT e.id, e.merged_into FROM entity e WHERE e.owner = $1 AND e.merged_into IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM entity_alias a WHERE a.owner = e.owner AND a.entity_id = e.merged_into "
        "AND a.surface_norm = e.name_norm AND a.state = 'live' AND a.source IN ('equation', 'manual'))", owner)
    if not rows:
        return set()
    affected: set[int] = set()
    for r in rows:
        await conn.execute("UPDATE entity SET merged_into = NULL WHERE owner = $1 AND id = $2", owner, r["id"])
        affected |= {int(r["id"]), int(r["merged_into"])}
    return await reresolve(conn, owner, affected)


async def delete_spans_on_conn(conn, owner: str, span_ids: Sequence[int]) -> int:
    """Delete one owner's spans and rebuild what they supported (spec §4.2 Deletion).

    Must run inside a transaction. Evidence, alias rows and turn_entity rows
    cascade; uptakes are removed first (RESTRICT). AGE Turn vertices are not
    touched here.
    """
    owned = [int(r["id"]) for r in await conn.fetch(
        "SELECT id FROM conversations WHERE agent_identity = $1 AND id = ANY($2::bigint[])",
        owner, [int(i) for i in span_ids])]
    if not owned:
        return 0
    keys: set[TimelineKey] = set()
    for r in await conn.fetch(
            "SELECT DISTINCT subject_id, verb, object_key FROM claim_evidence "
            "WHERE owner = $1 AND span_id = ANY($2::bigint[])", owner, owned):
        keys.add(timeline_key(owner, int(r["subject_id"]), await verb_row(conn, r["verb"]), r["object_key"]))
    # Lock distinct timelines in sorted order before evidence is removed or rewritten.
    for lock_owner, subject_id, verb in sorted({(k.owner, k.subject_id, k.verb) for k in keys}):
        await lock_timeline(conn, lock_owner, subject_id, verb)
    await conn.execute("DELETE FROM uptakes WHERE prior_span_id = ANY($1::bigint[]) "
                       "OR next_span_id = ANY($1::bigint[])", owned)
    await conn.execute("UPDATE claims SET span_id = NULL WHERE span_id = ANY($1::bigint[])", owned)
    await conn.execute("DELETE FROM memory_chunk_nodes WHERE source = 'conversation' "
                       "AND chunk_id = ANY($1::text[])", [f"conv_{i}" for i in owned])
    status = await conn.execute("DELETE FROM conversations WHERE agent_identity = $1 AND id = ANY($2::bigint[])",
                                owner, owned)
    keys |= await unmerge_unjustified(conn, owner)
    await rebuild_keys(conn, keys)
    return int(str(status).split()[-1])


async def claims_counts_on_conn(conn) -> dict[str, int]:
    rows = await conn.fetch("SELECT coalesce(status, 'unmigrated') AS s, count(*)::int AS n FROM claims GROUP BY 1")
    return {r["s"]: int(r["n"]) for r in rows}


async def drift_keys(conn, limit: int = 500) -> set[TimelineKey]:
    """Keys whose stored rows disagree with their evidence (raw-SQL deletes, crashes)."""
    keys: set[TimelineKey] = set()
    rows = await conn.fetch(
        f"""
        SELECT c.owner, c.subject_id, c.verb, {OBJECT_KEY_SQL} AS object_key FROM claims c
         WHERE c.status IN ('grounded', 'unconfirmed') AND c.subject_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.owner = c.owner
                             AND e.claim_id = c.claim_id AND e.state = 'live')
        UNION
        SELECT e.owner, e.subject_id, e.verb, e.object_key FROM claim_evidence e
          JOIN claims c ON c.owner = e.owner AND c.claim_id = e.claim_id
         WHERE e.state = 'live' AND c.status = 'orphaned'
        LIMIT $1
        """, int(limit))
    for r in rows:
        keys.add(timeline_key(r["owner"], int(r["subject_id"]), await verb_row(conn, r["verb"]), r["object_key"]))
    return keys


async def repair_drift_on_conn(conn) -> int:
    keys = await drift_keys(conn)
    await rebuild_keys(conn, keys)
    return len(keys)


FACT_VEC_DIM = 768


async def claims_missing_vectors(conn, owner: str, claim_ids: Sequence[int]) -> list[tuple[int, str]]:
    """(claim_id, fact_text) for this owner's grounded current claims that still lack a vector."""
    if not claim_ids:
        return []
    rows = await conn.fetch(
        "SELECT claim_id, fact_text FROM claims WHERE owner = $1 AND claim_id = ANY($2::bigint[]) "
        "AND status = 'grounded' AND valid_to IS NULL AND fact_text IS NOT NULL AND fact_vec IS NULL "
        "ORDER BY claim_id", owner, [int(i) for i in claim_ids])
    return [(int(r["claim_id"]), r["fact_text"]) for r in rows]


async def write_fact_vectors(conn, rows: Sequence[tuple[int, list[float]]], embed_model: str) -> int:
    """Store fact vectors and stamp embed_model; returns how many claims were updated.

    Every vector is length-checked before any write. Only rows still eligible
    (grounded, current, text present, vector empty) are touched, so a claim that
    was superseded after its text was read keeps a null vector. Never writes `valid`.
    """
    for claim_id, vec in rows:
        if len(vec) != FACT_VEC_DIM:
            raise ValueError(f"fact vector for claim {claim_id} has {len(vec)} dimensions, expected {FACT_VEC_DIM}")
    from .embed import vec_to_literal
    written = 0
    async with conn.transaction():
        for claim_id, vec in rows:
            status = await conn.execute(
                "UPDATE claims SET fact_vec = $2::vector, embed_model = $3 WHERE claim_id = $1 "
                "AND status = 'grounded' AND valid_to IS NULL AND fact_text IS NOT NULL AND fact_vec IS NULL",
                int(claim_id), vec_to_literal(vec), embed_model)
            written += int(str(status).split()[-1])
    return written


async def record_rule_claims_on_conn(conn, span_id: int, claims: Sequence[dict],
                                     extractor: str = RULES_EXTRACTOR, version: str = RULES_VERSION,
                                     *, speaker: str | None = None, seg_start: int | None = None,
                                     seg_end: int | None = None) -> list[int]:
    """Place one span's claims as evidence and rebuild their timelines on the caller's transaction.

    Distinct timeline locks are taken in sorted (owner, subject_id, verb) order
    before any evidence is placed. Returns the claim ids the evidence now backs.
    """
    info = await span_info(conn, span_id)
    locks = set()
    for c in claims:
        ident = await _claim_lock_identity(conn, info, c)
        if ident is not None:
            locks.add(ident)
    for owner, subject_id, verb in sorted(locks):
        await lock_timeline(conn, owner, subject_id, verb)
    added = [await add_evidence(conn, info, span_id, c, extractor=extractor, version=version,
                                speaker=speaker, seg_start=seg_start, seg_end=seg_end)
             for c in claims]
    kept = [a for a in added if a is not None]
    if not kept:
        return []
    await rebuild_keys(conn, [k for _, k in kept])
    ev_ids = [e for e, _ in kept]
    await _write_turn_entities(conn, info.owner, span_id, ev_ids)
    owner_of = {int(r["id"]): int(r["claim_id"]) for r in await conn.fetch(
        "SELECT id, claim_id FROM claim_evidence WHERE id = ANY($1::bigint[])", ev_ids)}
    return [owner_of[e] for e in ev_ids]


class StoreClaimsMixin:
    """Pool-holding wrappers over the module functions."""

    pool: Any

    async def claim_vector_search(self, owner: str, vec_literal: str, limit: int = 24) -> list[dict]:
        if limit <= 0:
            return []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                rows = await conn.fetch(
                    """
                    SELECT claim_id, subject_id, object_id, fact_text, fact_vec::text AS fact_vec,
                           span_id, valid_from,
                           1 - (fact_vec <=> $2::vector) AS similarity
                      FROM claims
                     WHERE owner = $1 AND status = 'grounded' AND valid_to IS NULL
                       AND fact_vec IS NOT NULL AND act = 'assert'
                     ORDER BY fact_vec <=> $2::vector
                     LIMIT $3
                    """,
                    owner, vec_literal, int(limit),
                )
        return [dict(row) for row in rows]

    async def link_entities(
        self, owner: str, phrases: Sequence[str], vec_literal: str | None = None, limit: int = 20,
    ) -> list[dict]:
        if not phrases and not vec_literal:
            return []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                rows = await conn.fetch(
                    """
                    WITH terms AS (
                        SELECT DISTINCT unnest($2::text[]) AS term
                    ),
                    candidates AS (
                        SELECT coalesce(e.merged_into, e.id) AS entity_id,
                               CASE WHEN a.surface_norm = t.term THEN 2 ELSE 1 END AS exact,
                               greatest(similarity(a.surface_norm, t.term),
                                        similarity(e.name_norm, t.term)) AS trigram
                          FROM terms t
                          JOIN entity_alias a ON a.owner = $1 AND a.state = 'live'
                           AND (a.surface_norm = t.term OR a.surface_norm % t.term)
                          JOIN entity e ON e.owner = a.owner AND e.id = a.entity_id
                        UNION ALL
                        SELECT coalesce(e.merged_into, e.id) AS entity_id,
                               CASE WHEN e.name_norm = t.term THEN 2 ELSE 1 END AS exact,
                               similarity(e.name_norm, t.term) AS trigram
                          FROM terms t
                          JOIN entity e ON e.owner = $1
                           AND (e.name_norm = t.term OR e.name_norm % t.term)
                    ),
                    ranked AS (
                        SELECT entity_id, max(exact) AS exact, max(trigram) AS trigram
                          FROM candidates
                         GROUP BY entity_id
                    ),
                    name_vectors AS (
                        SELECT id AS entity_id, 0 AS exact, 0::real AS trigram,
                               1 - (name_vec <=> $3::vector) AS name_similarity
                          FROM entity
                         WHERE owner = $1 AND merged_into IS NULL
                           AND name_vec IS NOT NULL AND $3::vector IS NOT NULL
                         ORDER BY name_vec <=> $3::vector
                         LIMIT 3
                    ),
                    combined AS (
                        SELECT entity_id, exact, trigram, 0::real AS name_similarity
                          FROM ranked
                         WHERE exact = 2 OR trigram >= 0.6
                        UNION ALL
                        SELECT entity_id, exact, trigram, name_similarity
                          FROM name_vectors
                         WHERE name_similarity >= 0.8
                    )
                    SELECT e.id AS entity_id, e.name, max(c.exact) AS exact,
                           max(c.trigram) AS trigram, max(c.name_similarity) AS name_similarity
                      FROM combined c
                      JOIN entity e ON e.owner = $1 AND e.id = c.entity_id
                     WHERE e.merged_into IS NULL
                     GROUP BY e.id, e.name
                     ORDER BY max(c.exact) DESC, max(c.name_similarity) DESC,
                              max(c.trigram) DESC, e.id
                     LIMIT $4
                    """,
                    owner, list(phrases), vec_literal, int(limit),
                )
        return [dict(row) for row in rows]

    async def turn_entities(self, owner: str, span_ids: Sequence[int]) -> list[dict]:
        ids = sorted({int(value) for value in span_ids})
        if not ids:
            return []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                rows = await conn.fetch(
                    """
                    SELECT te.span_id, te.entity_id, e.name
                      FROM turn_entity te
                      JOIN entity e ON e.owner = te.owner AND e.id = te.entity_id
                     WHERE te.owner = $1 AND te.span_id = ANY($2::bigint[])
                       AND e.merged_into IS NULL
                     ORDER BY te.span_id, te.entity_id
                    """,
                    owner, ids,
                )
        return [dict(row) for row in rows]

    async def claims_for_turns(self, owner: str, span_ids: Sequence[int]) -> list[str]:
        ids = sorted({int(value) for value in span_ids})
        if not ids:
            return []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT c.claim_id
                      FROM claims c
                      JOIN claim_evidence ev ON ev.owner = c.owner AND ev.claim_id = c.claim_id
                     WHERE c.owner = $1 AND c.status = 'grounded' AND c.valid_to IS NULL
                       AND ev.state = 'live' AND ev.act = 'assert'
                       AND ev.span_id = ANY($2::bigint[])
                     ORDER BY c.claim_id
                    """,
                    owner, ids,
                )
        return [str(row["claim_id"]) for row in rows]

    async def claim_edges(
        self, owner: str, entity_ids: Sequence[int], vec_literal: str, per_node: int = 8,
    ) -> list[dict]:
        ids = sorted({int(value) for value in entity_ids})
        if not ids or per_node <= 0:
            return []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                rows = await conn.fetch(
                    """
                    WITH frontier AS (SELECT unnest($2::bigint[]) AS entity_id),
                    candidates AS (
                        SELECT f.entity_id AS source_id, c.claim_id,
                               c.object_id AS neighbour_id, c.fact_text,
                               c.fact_vec::text AS fact_vec
                          FROM frontier f
                          JOIN claims c ON c.owner = $1 AND c.subject_id = f.entity_id
                         WHERE c.status = 'grounded' AND c.valid_to IS NULL
                           AND c.object_id IS NOT NULL AND c.fact_vec IS NOT NULL
                        UNION ALL
                        SELECT f.entity_id AS source_id, c.claim_id,
                               c.subject_id AS neighbour_id, c.fact_text,
                               c.fact_vec::text AS fact_vec
                          FROM frontier f
                          JOIN claims c ON c.owner = $1 AND c.object_id = f.entity_id
                         WHERE c.status = 'grounded' AND c.valid_to IS NULL
                           AND c.subject_id IS NOT NULL AND c.fact_vec IS NOT NULL
                    ),
                    edges AS (
                        SELECT *, row_number() OVER (
                            PARTITION BY source_id
                            ORDER BY fact_vec::vector <=> $3::vector, claim_id
                        ) AS node_rank
                          FROM candidates
                    )
                    SELECT source_id, claim_id, neighbour_id, fact_text, fact_vec
                      FROM edges
                     WHERE node_rank <= $4
                     ORDER BY source_id, node_rank, claim_id
                    """,
                    owner, ids, vec_literal, int(per_node),
                )
        return [dict(row) for row in rows]

    async def claims_by_ids(self, owner: str, claim_ids: Sequence[int | str]) -> list[dict]:
        ids = sorted({int(value) for value in claim_ids})
        if not ids:
            return []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                rows = await conn.fetch(
                    """
                    SELECT c.claim_id, c.fact_text, c.fact_vec::text AS fact_vec,
                           c.subject, c.verb, c.object, c.status,
                           ev.span_id, ev.event_time, cv.role, cv.content, cv.ts, cv.session_id
                      FROM claims c
                      JOIN claim_evidence ev ON ev.owner = c.owner AND ev.claim_id = c.claim_id
                      JOIN conversations cv ON cv.agent_identity = ev.owner AND cv.id = ev.span_id
                     WHERE c.owner = $1 AND c.claim_id = ANY($2::bigint[])
                       AND c.status = 'grounded' AND c.valid_to IS NULL
                       AND ev.state = 'live' AND ev.act = 'assert'
                     ORDER BY c.claim_id, ev.event_time, ev.span_id
                    """,
                    owner, ids,
                )
        return [dict(row) for row in rows]

    async def delete_spans(self, owner: str, span_ids: Sequence[int]) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await delete_spans_on_conn(conn, owner, span_ids)

    async def ensure_entity(self, owner: str, surface: str, type_: str = "other") -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await ensure_entity(conn, owner, surface, type_)

    async def resolve_entity(self, owner: str, surface: str) -> Optional[int]:
        async with self.pool.acquire() as conn:
            return await resolve_entity(conn, owner, surface)

    async def record_rule_claims(self, span_id: int, claims: Sequence[dict], *,
                                 extractor: str = RULES_EXTRACTOR, version: str = RULES_VERSION,
                                 speaker: str | None = None, seg_start: int | None = None,
                                 seg_end: int | None = None) -> list[int]:
        """Publish one span's extracted claims as evidence and rebuild their timelines.

        One transaction (spec §5.2 step 3, Phase 1 inline form). Abstained
        surfaces and unknown verbs are skipped; a re-run adds nothing. Before
        any evidence is placed, the distinct timeline locks for the batch are
        taken in sorted (owner, subject_id, verb) order, so two spans naming
        the same keys in opposite order do not deadlock. The retry on a
        Postgres deadlock abort is only a backstop, not the ordering mechanism.
        """
        for attempt in range(3):
            try:
                return await self._record_rule_claims_once(
                    span_id, claims, extractor, version, speaker, seg_start, seg_end)
            except asyncpg.exceptions.DeadlockDetectedError:
                if attempt == 2:
                    raise
        return []

    async def _record_rule_claims_once(self, span_id, claims, extractor, version,
                                       speaker=None, seg_start=None, seg_end=None) -> list[int]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await record_rule_claims_on_conn(
                    conn, span_id, claims, extractor, version,
                    speaker=speaker, seg_start=seg_start, seg_end=seg_end)

    async def record_repair(self, prior_span_id: int, repair_span_id: int, *, deictic: bool,
                            new_claims: Sequence[dict] = ()) -> int:
        """User retract evidence for the prior span's claims this repair contradicts.

        Deictic: every claim the prior span asserted. Otherwise: prior-span
        claims with the same subject, verb and object as a new claim and the
        opposite polarity. Same owner only; idempotent by the evidence key.
        Distinct timeline locks are taken in sorted (owner, subject_id, verb)
        order before any evidence or placeholder is written.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                info = await span_info(conn, repair_span_id)
                prior = await span_info(conn, prior_span_id)
                if info.role != "user" or prior.owner != info.owner:
                    return 0
                rows = await conn.fetch(
                    "SELECT DISTINCT subject_id, verb, object_key, polarity, subject_surface, object_surface "
                    "FROM claim_evidence WHERE owner = $1 AND span_id = $2 AND state = 'live' AND act = 'assert'",
                    info.owner, int(prior_span_id))
                if not deictic:
                    contradicted = set()
                    for c in new_claims:
                        sid = await resolve_entity(conn, info.owner, str(c.get("subject") or ""))
                        vrec = await verb_row(conn, str(c.get("verb") or ""))
                        okey = await object_key_for(conn, info.owner, str(c.get("object") or ""))
                        if sid is None or vrec is None or okey is None:
                            continue
                        flipped = "negative" if (c.get("polarity") or "positive") == "positive" else "positive"
                        contradicted.add((sid, vrec["verb"], okey, flipped))
                    rows = [r for r in rows if (int(r["subject_id"]), r["verb"], r["object_key"], r["polarity"])
                            in contradicted]
                for owner, subject_id, verb in sorted({(info.owner, int(r["subject_id"]), r["verb"])
                                                       for r in rows}):
                    await lock_timeline(conn, owner, subject_id, verb)
                written, keys = 0, []
                for r in rows:
                    vrec = await verb_row(conn, r["verb"])
                    await lock_timeline(conn, info.owner, int(r["subject_id"]), r["verb"])
                    holder = await placeholder_claim(conn, info.owner, int(r["subject_id"]), r["verb"],
                                                     r["object_key"], r["polarity"], repair_span_id)
                    ev = await conn.fetchval(
                        "INSERT INTO claim_evidence (owner, claim_id, span_id, speaker, act, polarity, "
                        "subject_surface, object_surface, subject_id, verb, object_key, extractor, "
                        "extractor_version, event_time) VALUES ($1, $2, $3, 'user', 'retract', $4, $5, $6, $7, "
                        "$8, $9, $10, $11, $12) ON CONFLICT (owner, span_id, extractor, extractor_version, "
                        "subject_id, verb, object_key, polarity, act) WHERE state = 'live' DO NOTHING RETURNING id",
                        info.owner, holder, int(repair_span_id), r["polarity"], r["subject_surface"],
                        r["object_surface"], int(r["subject_id"]), r["verb"], r["object_key"],
                        RULES_EXTRACTOR, RULES_VERSION, info.ts)
                    if ev is not None:
                        written += 1
                    keys.append(timeline_key(info.owner, int(r["subject_id"]), vrec, r["object_key"]))
                await rebuild_keys(conn, keys)
                return written

    async def record_alias_equation(self, span_id: int, canon_surface: str, alias_surface: str) -> bool:
        """Explicit user "X aka Y" (spec §4.1). Attach Y to X; merge when Y names another entity.

        The newer entity merges into the older. The merge is justified by a live
        equation row (merged.name_norm -> survivor); restating an equation moves
        that row to the newer span, so deleting the newer span un-merges.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                info = await span_info(conn, span_id)
                if info.role != "user" or is_abstained(canon_surface) or is_abstained(alias_surface):
                    return False
                canon_id = await ensure_entity(conn, info.owner, canon_surface)
                other = await resolve_entity(conn, info.owner, alias_surface)
                await upsert_alias(conn, info.owner, normalize_name(alias_surface), canon_id, "equation",
                                   span_id, info.ts)
                if other is None or other == canon_id:
                    return True
                types = {r["id"]: r["type"] for r in await conn.fetch(
                    "SELECT id, type FROM entity WHERE owner = $1 AND id = ANY($2::bigint[])",
                    info.owner, [canon_id, other])}
                if "self" in types.values():
                    return True
                survivor, merged = sorted((canon_id, other))
                merged_norm = await conn.fetchval(
                    "SELECT name_norm FROM entity WHERE owner = $1 AND id = $2", info.owner, merged)
                await upsert_alias(conn, info.owner, merged_norm, survivor, "equation", span_id, info.ts)
                await conn.execute("UPDATE entity SET merged_into = $3 WHERE owner = $1 AND id = $2",
                                   info.owner, merged, survivor)
                await rebuild_keys(conn, await reresolve(conn, info.owner, {merged, survivor}))
                return True

    async def claims_counts(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            return await claims_counts_on_conn(conn)

    async def repair_drift(self) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await repair_drift_on_conn(conn)
