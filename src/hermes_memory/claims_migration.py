"""One-shot legacy claims → evidence migration (graph layer spec §7.2 steps 2–6).

Runs once per database (data_migrations 'claims_evidence_v1') under an
advisory lock, in one transaction, after V18. Pre-migration truth is
claims.legacy_valid; legacy_check compares it per value afterwards.

Lock order: every legacy row is resolved first (entities, object keys), then
the distinct (owner, subject_id, verb) timelines are locked in sorted order,
and only then is evidence inserted and the timelines rebuilt.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import timedelta

from .entities import is_abstained
from .store_claims import (
    ensure_entity,
    lock_timeline,
    object_key_for,
    rebuild_keys,
    timeline_key,
    upsert_alias,
    verb_row,
)

logger = logging.getLogger("hermes_memory.claims_migration")
NAME = "claims_evidence_v1"
LEGACY_VERSION = "legacy"

_LEGACY_ROWS = """
SELECT c.claim_id, c.span_id, c.subject, c.verb, c.object, c.polarity, c.legacy_valid, c.superseded_by,
       c.subject_canon, c.object_canon, cv.agent_identity AS owner, cv.ts, cv.role
  FROM claims c JOIN conversations cv ON cv.id = c.span_id
 WHERE c.legacy_valid IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.owner = c.owner AND e.claim_id = c.claim_id)
 ORDER BY c.claim_id
"""

_EVIDENCE = """
INSERT INTO claim_evidence (owner, claim_id, span_id, speaker, act, polarity, subject_surface,
    object_surface, subject_id, verb, object_key, extractor, extractor_version, event_time)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'rules', 'legacy', $12)
ON CONFLICT (owner, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act)
WHERE state = 'live' DO NOTHING
"""


async def _copy_aliases(conn, owner: str) -> int:
    from .store import Store

    rows = await conn.fetch("SELECT surface_norm, canon_id, created_at, alias_id FROM aliases WHERE valid = 'live'")
    amap, meta = Store._graph(rows)
    copied = 0
    for r in rows:
        canon, _ = Store._resolve(r["canon_id"], amap, meta)
        if is_abstained(canon) or is_abstained(r["surface_norm"]) or canon == r["surface_norm"]:
            continue
        await upsert_alias(conn, owner, r["surface_norm"], await ensure_entity(conn, owner, canon),
                           "migration", None, None)
        copied += 1
    return copied


async def _retract_site(conn, r, by_id) -> tuple[int, object]:
    sup = by_id.get(r["superseded_by"]) if r["superseded_by"] is not None else None
    if sup is not None and sup["owner"] == r["owner"]:
        return int(sup["span_id"]), sup["ts"]
    rep = await conn.fetchrow(
        "SELECT u.next_span_id, cv.ts FROM uptakes u JOIN conversations cv ON cv.id = u.next_span_id "
        "WHERE u.prior_span_id = $1 AND u.value = 'repaired' AND cv.agent_identity = $2 "
        "ORDER BY u.next_span_id LIMIT 1", r["span_id"], r["owner"])
    if rep is not None:
        return int(rep["next_span_id"]), rep["ts"]
    return int(r["span_id"]), r["ts"] + timedelta(microseconds=1)


async def _resolve_row(conn, r):
    """Resolve one legacy row to its plan; None when it can only be orphaned."""
    vrec = await verb_row(conn, r["verb"])
    object_key = None if is_abstained(r["object"]) else await object_key_for(conn, r["owner"], r["object"])
    if vrec is None or is_abstained(r["subject"]) or object_key is None:
        return None
    subject_id = await ensure_entity(conn, r["owner"], r["subject"])
    oid = int(object_key[2:]) if object_key.startswith("e:") else None
    canon = (await conn.fetchval("SELECT name_norm FROM entity WHERE owner = $1 AND id = $2",
                                 r["owner"], oid)) if oid is not None else object_key[2:]
    return {"vrec": vrec, "object_key": object_key, "subject_id": subject_id, "oid": oid, "canon": canon}


async def migrate_legacy_claims(store) -> dict[str, int]:
    async with store.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"hermes_memory:data:{NAME}")
            if await conn.fetchval("SELECT 1 FROM data_migrations WHERE name = $1", NAME):
                return {"skipped": 1}
            counts: Counter = Counter()
            owners = [r["agent_identity"] for r in await conn.fetch(
                "SELECT DISTINCT agent_identity FROM conversations")]
            if len(owners) == 1:
                counts["aliases_copied"] = await _copy_aliases(conn, owners[0])
            else:
                counts["aliases_dropped"] = int(await conn.fetchval(
                    "SELECT count(*) FROM aliases WHERE valid = 'live'"))
            rows = await conn.fetch(_LEGACY_ROWS)
            by_id = {int(r["claim_id"]): r for r in rows}

            # 1. Resolve every row before taking any timeline lock.
            plans = [(r, await _resolve_row(conn, r)) for r in rows]

            # 2. Lock the distinct timelines in one global sorted order.
            identities = sorted({(r["owner"], int(p["subject_id"]), p["vrec"]["verb"])
                                 for r, p in plans if p is not None})
            for owner, subject_id, verb in identities:
                await lock_timeline(conn, owner, subject_id, verb)

            # 3. Insert evidence and update claim rows.
            keys = set()
            for r, p in plans:
                if p is None:
                    await conn.execute("UPDATE claims SET status = 'orphaned' WHERE claim_id = $1", r["claim_id"])
                    counts["orphaned"] += 1
                    continue
                vrec, object_key, subject_id = p["vrec"], p["object_key"], p["subject_id"]
                await conn.execute(
                    "UPDATE claims SET status = 'orphaned', subject_id = $2, object_id = $3, object_canon = $4, "
                    "verb = $5 WHERE claim_id = $1", r["claim_id"], subject_id, p["oid"], p["canon"], vrec["verb"])
                speaker = "assistant" if r["role"] == "assistant" else "user"
                await conn.execute(_EVIDENCE, r["owner"], r["claim_id"], r["span_id"], speaker, "assert",
                                   r["polarity"], r["subject"], r["object"], subject_id, vrec["verb"],
                                   object_key, r["ts"])
                sup = by_id.get(r["superseded_by"]) if r["superseded_by"] is not None else None
                same_value = sup is not None and (sup["subject_canon"], sup["verb"], sup["object_canon"],
                                                  sup["polarity"]) == (r["subject_canon"], r["verb"],
                                                                       r["object_canon"], r["polarity"])
                if r["legacy_valid"] == "retracted" and same_value:
                    counts["deduplicated"] += 1
                elif r["legacy_valid"] == "retracted":
                    site, when = await _retract_site(conn, r, by_id)
                    await conn.execute(_EVIDENCE, r["owner"], r["claim_id"], site, "user", "retract",
                                       r["polarity"], r["subject"], r["object"], subject_id, vrec["verb"],
                                       object_key, when)
                    counts["retracted"] += 1
                else:
                    counts[r["legacy_valid"]] += 1
                keys.add(timeline_key(r["owner"], subject_id, vrec, object_key))

            # 4. Rebuild.
            await rebuild_keys(conn, keys)
            await conn.execute("INSERT INTO data_migrations (name, detail) VALUES ($1, $2::jsonb)",
                               NAME, json.dumps(dict(counts)))
            return dict(counts)


_CHECK = """
WITH v AS (
  SELECT c.owner, c.subject_id, c.verb,
         CASE WHEN c.object_id IS NOT NULL THEN 'e:' || c.object_id::text ELSE 'l:' || c.object_canon END AS object_key,
         c.polarity,
         bool_or(c.legacy_valid = 'live') AS any_live,
         bool_and(c.legacy_valid = 'retracted') AS all_retracted
    FROM claims c
   WHERE c.legacy_valid IS NOT NULL AND c.subject_id IS NOT NULL
   GROUP BY 1, 2, 3, 4, 5)
SELECT v.*,
       EXISTS (SELECT 1 FROM claims g WHERE g.owner = v.owner AND g.subject_id = v.subject_id
                 AND g.verb = v.verb AND g.polarity = v.polarity AND g.status = 'grounded' AND g.valid_to IS NULL
                 AND (CASE WHEN g.object_id IS NOT NULL THEN 'e:' || g.object_id::text ELSE 'l:' || g.object_canon END)
                     = v.object_key) AS is_current,
       EXISTS (SELECT 1 FROM claims g WHERE g.owner = v.owner AND g.subject_id = v.subject_id
                 AND g.verb = v.verb AND g.status = 'grounded' AND g.valid_to IS NULL) AS key_has_current
  FROM v
"""


async def legacy_check(conn) -> dict[str, list[dict]]:
    """§7.2 step 6, per value. A live value replaced by a newer statement is listed, not a violation."""
    out: dict[str, list[dict]] = {"violations": [], "superseded": []}
    for r in await conn.fetch(_CHECK):
        row = {k: r[k] for k in ("owner", "subject_id", "verb", "object_key", "polarity")}
        if r["any_live"] and not r["is_current"]:
            (out["superseded"] if r["key_has_current"] else out["violations"]).append(row)
        elif r["all_retracted"] and r["is_current"]:
            out["violations"].append(row)
    return out
