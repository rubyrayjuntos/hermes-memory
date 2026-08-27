#!/usr/bin/env python3
"""librarian_health — scoped health watchdog for hermes-memory (issue #17).

Correct scoped query (file-backed only) — prevents false positives on
user prefs written via the generic memory tool (no file_path by design,
e.g. ids 1799-1801).

  WHERE metadata->>'file_path' IS NOT NULL
    AND (metadata->>'hash' IS NULL OR metadata->>'doc_type' IS NULL)

Wraps Store.librarian_health() and also emits raw psql counts for triage.
Exit 0 = healthy (scoped missing ==0), 1 = drift, 2 = DB unreachable.

Usage:
  python scripts/librarian_health.py              # JSON to stdout
  python scripts/librarian_health.py --fail-on-drift
  python scripts/librarian_health.py --dsn postgres://...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


async def run(dsn: str | None) -> dict:
    from hermes_memory.config import load_config
    from hermes_memory.store import Store
    import asyncpg

    cfg = load_config()
    pw = os.environ.get("HERMES_PG_PASSWORD", "")
    dsn = (
        dsn
        or os.environ.get("HYBRID_AGE_DSN")
        or cfg.dsn.replace("{pg_password}", pw).replace("***", pw)
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        store = Store(pool, graph_name=cfg.graph)
        health = await store.librarian_health()
        # also fetch naive counts for comparison
        async with pool.acquire() as conn:
            naive = await conn.fetchval("SELECT count(*) FROM memory_entries WHERE metadata->>'hash' IS NULL")
            total = await conn.fetchval("SELECT count(*) FROM memory_entries")
        health["naive_missing_hash"] = int(naive or 0)
        health["total_entries"] = int(total or 0)
        health["healthy"] = health["missing_hash"] == 0 and health["missing_doc_type"] == 0
        return health
    finally:
        await pool.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Librarian scoped health check (issue #17)")
    ap.add_argument("--dsn", default=None, help="Postgres DSN (default: HYBRID_AGE_DSN or config)")
    ap.add_argument("--fail-on-drift", action="store_true", help="Exit 1 if scoped missing >0")
    ap.add_argument("--json", dest="json_out", default=None, help="Write JSON to file (default: stdout)")
    args = ap.parse_args(argv)

    try:
        health = asyncio.run(run(args.dsn))
    except Exception as exc:
        print(f"librarian_health: DB unreachable: {exc}", file=sys.stderr)
        return 2

    out = json.dumps(health, indent=2)
    if args.json_out:
        with open(args.json_out, "w") as f:
            f.write(out + "\n")
    else:
        print(out)

    if args.fail_on_drift and not health.get("healthy"):
        print(
            f"DRIFT: file-backed missing_hash={health['missing_hash']} missing_doc_type={health['missing_doc_type']}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
