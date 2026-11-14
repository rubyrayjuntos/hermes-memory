"""Shared helpers for claims integration tests."""
from __future__ import annotations

import itertools

_n = itertools.count()


def t(minute: int) -> str:
    return f"2026-01-01T00:{minute:02d}:00+00:00"


async def span(store, owner: str, minute: int, role: str = "user", session: str = "s1") -> int:
    tid = await store.insert_turn(session, owner, role, f"turn {next(_n)} at {minute}", None, ts=t(minute))
    return int(tid)


async def current(db_pool, owner: str, subject_norm: str, verb: str) -> list[tuple[str, str]]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.object, c.polarity FROM claims c JOIN entity s ON s.owner = c.owner AND s.id = c.subject_id "
            "WHERE c.owner = $1 AND s.name_norm = $2 AND c.verb = $3 AND c.status = 'grounded' "
            "AND c.valid_to IS NULL ORDER BY c.claim_id", owner, subject_norm, verb)
    return [(r["object"], r["polarity"]) for r in rows]


async def history(db_pool, owner: str, subject_norm: str, verb: str) -> list[tuple[str, str, bool]]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.object, c.status, c.valid_to IS NULL AS is_current FROM claims c "
            "JOIN entity s ON s.owner = c.owner AND s.id = c.subject_id "
            "WHERE c.owner = $1 AND s.name_norm = $2 AND c.verb = $3 AND c.status = 'grounded' "
            "ORDER BY c.valid_from", owner, subject_norm, verb)
    return [(r["object"], r["status"], r["is_current"]) for r in rows]
