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
from typing import Optional

from .entities import SELF_NAME, SELF_NORM, is_abstained, is_first_person, normalize_name

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


class StoreClaimsMixin:
    """Pool-holding wrappers over the module functions."""

    pool: object

    async def ensure_entity(self, owner: str, surface: str, type_: str = "other") -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await ensure_entity(conn, owner, surface, type_)

    async def resolve_entity(self, owner: str, surface: str) -> Optional[int]:
        async with self.pool.acquire() as conn:
            return await resolve_entity(conn, owner, surface)
