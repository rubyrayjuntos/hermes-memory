"""Read models for the Graph Explorer UI (entities, claims, evidence, jobs)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# Demo UI historically used "retracted"; DB uses "withdrawn".
_EV_STATE_UI = {"withdrawn": "retracted"}

_SCHEMA = [
    {
        "table": "entity",
        "role": "Canonical named things per owner (person/project/place/…).",
        "keys": "PRIMARY KEY (owner, id); UNIQUE (owner, name_norm, type)",
    },
    {
        "table": "entity_alias",
        "role": "Surface forms → entity. Live aliases resolve lookup; equations justify merges.",
        "keys": "PRIMARY KEY (owner, surface_norm, entity_id)",
    },
    {
        "table": "turn_entity",
        "role": "Passport: which entities appeared on which conversation span.",
        "keys": "PRIMARY KEY (owner, span_id, entity_id)",
    },
    {
        "table": "claims",
        "role": "Projected facts (subject_id, verb, object). Status + valid_from/to are the timeline.",
        "keys": "claim_id; claims_current_key on live current rows",
    },
    {
        "table": "claim_evidence",
        "role": "Why a claim exists — one row per assert/retract event. Claims are a projection of this.",
        "keys": "id; unique live event key on (span, extractor, subject, verb, object_key, …)",
    },
    {
        "table": "verb",
        "role": "Verb catalog; functional=true verbs keep one live interval at a time.",
        "keys": "verb PRIMARY KEY",
    },
    {
        "table": "durable_job",
        "role": "Lease+fence worker queue (capture/extract/…).",
        "keys": "UNIQUE (owner, kind, op_id)",
    },
    {
        "table": "conversations",
        "role": "Spans (turns). Claims and evidence point here via span_id.",
        "keys": "id; scoped by agent_identity (= owner)",
    },
]


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _date(value: Any) -> str:
    iso = _iso(value)
    return iso[:10] if iso else ""


def _lease_left(expires_at: Any) -> str:
    if expires_at is None:
        return "—"
    if not isinstance(expires_at, datetime):
        return "—"
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    secs = int((expires_at - now).total_seconds())
    if secs <= 0:
        return "expired"
    mins, rem = divmod(secs, 60)
    return f"{mins:02d}:{rem:02d} left"


def _excerpt(text: Any, *, limit: int = 280) -> str:
    if text is None:
        return ""
    s = " ".join(str(text).split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _row_dict(row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, datetime):
            out[k] = _iso(v)
        elif hasattr(v, "tolist"):  # numpy / vector-ish — stringify
            out[k] = str(v)
        else:
            try:
                # asyncpg UUID etc.
                out[k] = v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)
            except Exception:
                out[k] = str(v)
    return out


class StoreExplorerMixin:
    pool: Any

    async def explorer_owners(self) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT owner FROM (
                    SELECT owner FROM entity
                    UNION
                    SELECT owner FROM claims WHERE owner IS NOT NULL
                    UNION
                    SELECT owner FROM durable_job
                ) u
                ORDER BY owner
                """
            )
        return [str(r["owner"]) for r in rows]

    async def explorer_snapshot(self, owner: str, *, limit: int = 2000) -> dict[str, Any]:
        """Owner-scoped bundle for the Graph Explorer (UI shapes + storage inspector)."""
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("owner is required")
        lim = max(1, min(int(limit), 5000))
        async with self.pool.acquire() as conn:
            ent_rows = await conn.fetch(
                """
                SELECT e.id, e.owner, e.name, e.name_norm, e.type, e.merged_into, e.created_at,
                       (e.name_vec IS NOT NULL) AS has_name_vec
                  FROM entity e
                 WHERE e.owner = $1
                 ORDER BY (e.merged_into IS NOT NULL), e.name
                 LIMIT $2
                """,
                owner,
                lim,
            )
            alias_rows = await conn.fetch(
                """
                SELECT surface_norm, entity_id, source, state, span_id, event_time, created_at
                  FROM entity_alias
                 WHERE owner = $1
                 ORDER BY surface_norm
                 LIMIT $2
                """,
                owner,
                lim,
            )
            passport_rows = await conn.fetch(
                """
                SELECT span_id, entity_id, surface, conf
                  FROM turn_entity
                 WHERE owner = $1
                 ORDER BY span_id DESC, entity_id
                 LIMIT $2
                """,
                owner,
                lim,
            )
            claim_rows = await conn.fetch(
                """
                SELECT c.claim_id, c.owner, c.subject_id, c.verb, c.object_id,
                       c.object_canon, c.object, c.subject, c.subject_canon,
                       c.polarity, c.act, c.status, c.fact_text, c.span_id,
                       c.valid_from, c.valid_to, c.superseded_by, c.legacy_valid,
                       c.uptake, c.seen_count, c.created_at, c.embed_model,
                       (c.fact_vec IS NOT NULL) AS has_fact_vec,
                       coalesce(
                         (SELECT min(ev.event_time) FROM claim_evidence ev
                           WHERE ev.owner = c.owner AND ev.claim_id = c.claim_id
                             AND ev.state = 'live' AND ev.act = 'assert'),
                         c.valid_from, c.created_at
                       ) AS event_time
                  FROM claims c
                 WHERE c.owner = $1 AND c.subject_id IS NOT NULL
                 ORDER BY c.claim_id
                 LIMIT $2
                """,
                owner,
                lim,
            )
            claim_ids = [int(r["claim_id"]) for r in claim_rows]
            ev_rows = await conn.fetch(
                """
                SELECT id, claim_id, span_id, speaker, act, polarity,
                       subject_surface, object_surface, subject_id, verb, object_key,
                       extractor, extractor_version, confidence, event_time, state, created_at
                  FROM claim_evidence
                 WHERE owner = $1 AND claim_id = ANY($2::bigint[])
                 ORDER BY claim_id, event_time, id
                """,
                owner,
                claim_ids,
            ) if claim_ids else []
            job_rows = await conn.fetch(
                """
                SELECT id, kind, op_id, status, fence, lease_expires_at, attempts,
                       owner, coalesce(last_error, '') AS last_error,
                       session_id, priority, claimed_by, created_at, event_time,
                       next_attempt_at, payload
                  FROM durable_job
                 WHERE owner = $1
                 ORDER BY id DESC
                 LIMIT $2
                """,
                owner,
                min(lim, 500),
            )
            verb_rows = await conn.fetch(
                "SELECT verb, functional, phrase, neg_phrase FROM verb ORDER BY verb"
            )
            span_ids: set[int] = set()
            for r in claim_rows:
                if r["span_id"] is not None:
                    span_ids.add(int(r["span_id"]))
            for r in ev_rows:
                if r["span_id"] is not None:
                    span_ids.add(int(r["span_id"]))
            for r in passport_rows:
                span_ids.add(int(r["span_id"]))
            for r in alias_rows:
                if r["span_id"] is not None:
                    span_ids.add(int(r["span_id"]))
            span_list = sorted(span_ids)[:lim]
            span_rows = await conn.fetch(
                """
                SELECT id, session_id, role, content, ts, drain_status, agent_identity
                  FROM conversations
                 WHERE agent_identity = $1 AND id = ANY($2::bigint[])
                """,
                owner,
                span_list,
            ) if span_list else []
            counts = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*)::int FROM entity WHERE owner = $1) AS entity,
                  (SELECT count(*)::int FROM entity WHERE owner = $1 AND merged_into IS NOT NULL) AS entity_merged,
                  (SELECT count(*)::int FROM entity_alias WHERE owner = $1) AS entity_alias,
                  (SELECT count(*)::int FROM turn_entity WHERE owner = $1) AS turn_entity,
                  (SELECT count(*)::int FROM claims WHERE owner = $1) AS claims,
                  (SELECT count(*)::int FROM claim_evidence WHERE owner = $1) AS claim_evidence,
                  (SELECT count(*)::int FROM durable_job WHERE owner = $1) AS durable_job,
                  (SELECT count(*)::int FROM conversations WHERE agent_identity = $1) AS conversations
                """,
                owner,
            )

        aliases_by_entity: dict[str, list[dict[str, Any]]] = {}
        for r in alias_rows:
            eid = str(r["entity_id"])
            aliases_by_entity.setdefault(eid, []).append(
                {
                    "surface_norm": str(r["surface_norm"]),
                    "source": str(r["source"]),
                    "state": str(r["state"]),
                    "span_id": str(r["span_id"]) if r["span_id"] is not None else None,
                    "event_time": _iso(r["event_time"]),
                }
            )

        passports_by_entity: dict[str, list[dict[str, Any]]] = {}
        for r in passport_rows:
            eid = str(r["entity_id"])
            passports_by_entity.setdefault(eid, []).append(
                {
                    "span_id": str(r["span_id"]),
                    "surface": str(r["surface"]),
                    "conf": float(r["conf"] or 0),
                }
            )

        spans: dict[str, dict[str, Any]] = {}
        for r in span_rows:
            sid = str(r["id"])
            spans[sid] = {
                "id": sid,
                "session_id": str(r["session_id"] or ""),
                "role": str(r["role"] or ""),
                "excerpt": _excerpt(r["content"]),
                "content_len": len(r["content"] or ""),
                "ts": _iso(r["ts"]),
                "drain_status": r["drain_status"],
            }

        evidence_by_claim: dict[int, list[dict[str, Any]]] = {}
        evidence_raw_by_claim: dict[int, list[dict[str, Any]]] = {}
        for r in ev_rows:
            cid = int(r["claim_id"])
            state = str(r["state"])
            evidence_by_claim.setdefault(cid, []).append(
                {
                    "id": str(r["id"]),
                    "span_id": str(r["span_id"]),
                    "extractor": str(r["extractor"]),
                    "version": str(r["extractor_version"]),
                    "speaker": str(r["speaker"]),
                    "act": str(r["act"]),
                    "polarity": str(r["polarity"]),
                    "subject_surface": str(r["subject_surface"]),
                    "object_surface": str(r["object_surface"]),
                    "object_key": str(r["object_key"]),
                    "confidence": float(r["confidence"] or 0),
                    "event_time": _iso(r["event_time"]) or "",
                    "state": _EV_STATE_UI.get(state, state),
                    "state_db": state,
                }
            )
            evidence_raw_by_claim.setdefault(cid, []).append(_row_dict(r))

        entities = []
        for r in ent_rows:
            eid = str(r["id"])
            etype_db = str(r["type"])
            etype = "person" if etype_db == "self" else etype_db
            if etype not in (
                "person", "project", "place", "org", "tool", "concept", "other",
            ):
                etype = "other"
            alias_detail = aliases_by_entity.get(eid, [])
            entities.append(
                {
                    "id": eid,
                    "owner": str(r["owner"]),
                    "name": str(r["name"]),
                    "name_norm": str(r["name_norm"]),
                    "type": etype,
                    "type_db": etype_db,
                    "aliases": [a["surface_norm"] for a in alias_detail if a["state"] == "live"],
                    "alias_detail": alias_detail,
                    "merged_into": (
                        str(r["merged_into"]) if r["merged_into"] is not None else None
                    ),
                    "has_name_vec": bool(r["has_name_vec"]),
                    "created_at": _iso(r["created_at"]),
                    "passports": passports_by_entity.get(eid, []),
                    "note": "",
                    "raw": {
                        "id": int(r["id"]),
                        "owner": str(r["owner"]),
                        "name": str(r["name"]),
                        "name_norm": str(r["name_norm"]),
                        "type": etype_db,
                        "merged_into": (
                            int(r["merged_into"]) if r["merged_into"] is not None else None
                        ),
                        "has_name_vec": bool(r["has_name_vec"]),
                        "created_at": _iso(r["created_at"]),
                    },
                }
            )

        claims = []
        for r in claim_rows:
            cid = int(r["claim_id"])
            object_id = r["object_id"]
            if object_id is not None:
                object_type = "entity"
                object_literal = None
                object_id_s = str(object_id)
            else:
                object_type = "literal"
                object_literal = str(r["object"] or r["object_canon"] or "")
                object_id_s = None
            status = str(r["status"] or "orphaned")
            span_id = str(r["span_id"]) if r["span_id"] is not None else ""
            claims.append(
                {
                    "id": str(cid),
                    "owner": str(r["owner"]),
                    "subject_id": str(r["subject_id"]),
                    "verb": str(r["verb"]),
                    "object_type": object_type,
                    "object_id": object_id_s,
                    "object_literal": object_literal,
                    "status": status,
                    "fact_text": str(r["fact_text"] or ""),
                    "event_date": _date(r["event_time"] or r["valid_from"]),
                    "span_id": span_id,
                    "span_excerpt": spans.get(span_id, {}).get("excerpt", ""),
                    "valid_from": _date(r["valid_from"]) or None,
                    "valid_to": _date(r["valid_to"]) or None,
                    "superseded_by": (
                        str(r["superseded_by"]) if r["superseded_by"] is not None else None
                    ),
                    "polarity": str(r["polarity"] or ""),
                    "uptake": str(r["uptake"] or ""),
                    "has_fact_vec": bool(r["has_fact_vec"]),
                    "embed_model": r["embed_model"],
                    "evidence": evidence_by_claim.get(cid, []),
                    "raw": {
                        **{k: v for k, v in _row_dict(r).items() if k != "event_time"},
                        "evidence": evidence_raw_by_claim.get(cid, []),
                    },
                }
            )

        jobs = []
        for r in job_rows:
            payload = r["payload"]
            if hasattr(payload, "items"):
                payload_preview = dict(payload)
            elif isinstance(payload, str):
                payload_preview = payload[:200]
            else:
                payload_preview = str(payload)[:200] if payload is not None else None
            jobs.append(
                {
                    "id": str(r["id"]),
                    "kind": str(r["kind"]),
                    "op_id": str(r["op_id"]),
                    "status": str(r["status"]),
                    "fence": f"f-{int(r['fence'])}",
                    "lease": _lease_left(r["lease_expires_at"]),
                    "attempts": int(r["attempts"] or 0),
                    "owner": str(r["owner"]),
                    "last_error": str(r["last_error"] or ""),
                    "session_id": str(r["session_id"] or ""),
                    "claimed_by": str(r["claimed_by"] or ""),
                    "priority": int(r["priority"] or 0),
                    "raw": {**_row_dict(r), "payload": payload_preview},
                }
            )

        count_map = {k: int(counts[k] or 0) for k in counts.keys()} if counts else {}
        schema = []
        for item in _SCHEMA:
            key = item["table"]
            schema.append({**item, "count": count_map.get(key, 0)})

        return {
            "owner": owner,
            "entities": entities,
            "claims": claims,
            "jobs": jobs,
            "spans": spans,
            "functional_verbs": [
                str(r["verb"]) for r in verb_rows if r["functional"]
            ],
            "verbs": [
                {
                    "verb": str(r["verb"]),
                    "functional": bool(r["functional"]),
                    "phrase": str(r["phrase"]),
                    "neg_phrase": str(r["neg_phrase"]),
                }
                for r in verb_rows
            ],
            "schema": schema,
            "counts": count_map,
            "source": "live",
        }
