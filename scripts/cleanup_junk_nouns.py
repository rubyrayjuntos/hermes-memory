#!/usr/bin/env python3
"""Idempotent #79 junk-noun cleanup.

Default is a review listing (no DELETE). --apply deletes matching noun rows;
V9 FKs CASCADE semantic_edge (src/tgt) and memory_chunk_nodes.noun_id.

The match is hermes_memory.extract_nouns.is_junk_fragment_label — the same
predicate as new extraction — not a one-shot id list. Re-run until the
candidate count is 0.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "src"))

from hermes_memory.config import dotenv_key_from_file, load_config  # noqa: E402
from hermes_memory.extract_nouns import is_junk_fragment_label  # noqa: E402
from hermes_memory.graph_api import _load_dotenv_files  # noqa: E402
from hermes_memory.schema_guard import UNPASSPORTED_TURNS_SQL  # noqa: E402


LIVE_DBS = frozenset({"hermes_memory", "hermes_memory_installed"})


def _resolve_dsn(raw: str) -> str:
    pw = os.environ.get("HERMES_PG_PASSWORD", "")
    if pw:
        raw = raw.replace("{pg_password}", pw).replace("***", pw)
    return raw


def _dsn_from_hermes_env() -> str:
    raw = dotenv_key_from_file(Path.home() / ".hermes" / ".env", "HYBRID_AGE_DSN")
    if not raw:
        raise SystemExit("missing HYBRID_AGE_DSN in ~/.hermes/.env")
    return _resolve_dsn(raw)


async def _fk_report(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT conname, pg_get_constraintdef(oid) AS def
          FROM pg_constraint
         WHERE conrelid IN ('semantic_edge'::regclass, 'memory_chunk_nodes'::regclass)
           AND contype = 'f'
           AND pg_get_constraintdef(oid) ILIKE '%noun%'
         ORDER BY conname
        """
    )
    return [f"{r['conname']}: {r['def']}" for r in rows]


async def _counts(conn: asyncpg.Connection) -> dict[str, int]:
    nouns = await conn.fetchval("SELECT count(*) FROM noun")
    edges = await conn.fetchval("SELECT count(*) FROM semantic_edge")
    passports = await conn.fetchval(
        """
        SELECT count(*) FROM memory_chunk_nodes
         WHERE source = 'conversation' AND noun_id IS NOT NULL
        """
    )
    unp = await conn.fetchval(UNPASSPORTED_TURNS_SQL)
    return {
        "nouns": int(nouns or 0),
        "edges": int(edges or 0),
        "passports": int(passports or 0),
        "unpassported": int(unp or 0),
    }


async def _passported_turn_ids(conn: asyncpg.Connection) -> set[int]:
    rows = await conn.fetch(
        """
        SELECT DISTINCT turn_id
          FROM memory_chunk_nodes
         WHERE source = 'conversation' AND noun_id IS NOT NULL
           AND turn_id IS NOT NULL
        """
    )
    return {int(r["turn_id"]) for r in rows}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="DELETE matching noun rows")
    ap.add_argument(
        "--dsn",
        default="",
        help="Optional DSN override (default HYBRID_AGE_DSN / load_config).",
    )
    ap.add_argument(
        "--installed",
        action="store_true",
        help="Target ~/.hermes/.env HYBRID_AGE_DSN (:5452 / hermes_memory_installed).",
    )
    args = ap.parse_args()
    _load_dotenv_files()
    if args.installed:
        dsn = _dsn_from_hermes_env()
    else:
        dsn = _resolve_dsn(
            args.dsn or os.environ.get("HYBRID_AGE_DSN") or load_config().dsn
        )
    parsed = urlparse(dsn)
    db = (parsed.path or "/").lstrip("/")
    if db not in LIVE_DBS:
        print(
            "refusing (database is not hermes_memory or hermes_memory_installed)",
            file=sys.stderr,
        )
        return 2
    target_label = "installed" if db == "hermes_memory_installed" else "live"
    conn = await asyncpg.connect(dsn)
    try:
        fks = await _fk_report(conn)
        print("database", target_label)
        print("fk")
        for line in fks:
            print(" ", line)
        before = await _counts(conn)
        print("before", " ".join(f"{k}={v}" for k, v in before.items()))
        rows = await conn.fetch("SELECT id, label FROM noun ORDER BY id")
        candidates = [
            (int(r["id"]), str(r["label"]))
            for r in rows
            if is_junk_fragment_label(str(r["label"]))
        ]
        print("candidates", len(candidates))
        for nid, lab in candidates:
            print(f"  {nid}\t{lab}")
        ids = [c[0] for c in candidates]
        affected: list[int] = []
        if ids:
            affected_rows = await conn.fetch(
                """
                SELECT DISTINCT x AS turn_id FROM (
                    SELECT turn_id AS x
                      FROM memory_chunk_nodes
                     WHERE noun_id = ANY($1::int[])
                       AND turn_id IS NOT NULL
                    UNION
                    SELECT unnest(provenance_turns) AS x
                      FROM semantic_edge
                     WHERE src_noun = ANY($1::int[])
                        OR tgt_noun = ANY($1::int[])
                ) t
                 WHERE x IS NOT NULL
                 ORDER BY 1
                """,
                ids,
            )
            affected = [int(r["turn_id"]) for r in affected_rows]
        print("affected_turns", len(affected), affected)
        if not args.apply:
            print("dry_run (pass --apply to delete)")
            return 0
        before_passported = await _passported_turn_ids(conn)
        deleted = 0
        if ids:
            deleted = int(
                await conn.fetchval(
                    "WITH d AS (DELETE FROM noun WHERE id = ANY($1::int[]) RETURNING 1) "
                    "SELECT count(*) FROM d",
                    ids,
                )
                or 0
            )
        after = await _counts(conn)
        after_passported = await _passported_turn_ids(conn)
        newly_unpassported = sorted(before_passported - after_passported)
        leftover = [
            (int(r["id"]), str(r["label"]))
            for r in await conn.fetch("SELECT id, label FROM noun")
            if is_junk_fragment_label(str(r["label"]))
        ]
        print("deleted_nouns", deleted)
        print("after", " ".join(f"{k}={v}" for k, v in after.items()))
        print("affected_turns", len(affected), affected)
        print("newly_unpassported", len(newly_unpassported), newly_unpassported)
        print("leftover_junk", len(leftover))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
