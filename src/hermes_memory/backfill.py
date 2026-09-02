"""hermes-memory-backfill — graph unlinked conversations via the provider linker.

Skips C5 verify synthetics. Does not resurrect extractors.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from .graph_api import is_verify_session
from .store import Store


async def run_backfill(dsn: Optional[str] = None, limit: int = 0) -> int:
    import asyncpg

    from .config import load_config
    from .embed import Embedder
    from .provider import HybridAgeMemoryProvider, _is_noise

    cfg = load_config()
    pw = os.environ.get("HERMES_PG_PASSWORD", "")
    dsn = (
        dsn
        or os.environ.get("HYBRID_AGE_DSN")
        or cfg.dsn.replace("{pg_password}", pw).replace("***", pw)
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    store = Store(pool, graph_name=cfg.graph)
    embedder = Embedder(cfg.embed_url, cfg.embed_model, cfg.embed_dim)
    provider = HybridAgeMemoryProvider(config=cfg)
    provider.store = store
    provider.embedder = embedder

    sql = """
        SELECT c.id, c.session_id, c.content, c.embedding::text AS embedding
          FROM conversations c
         WHERE NOT EXISTS (
                 SELECT 1 FROM memory_chunk_nodes b
                  WHERE b.chunk_id = 'conv_' || c.id::text
                    AND b.source = 'conversation'
               )
         ORDER BY c.id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    linked = 0
    skipped = 0
    for row in rows:
        if limit and linked >= limit:
            break
        sid = row["session_id"] or ""
        if is_verify_session(sid):
            skipped += 1
            continue
        content = row["content"] or ""
        if _is_noise(content):
            skipped += 1
            continue
        vec = None
        emb = row["embedding"]
        if emb:
            try:
                body = str(emb).strip()
                if body.startswith("[") and body.endswith("]"):
                    vec = [float(x) for x in body[1:-1].split(",") if x.strip()]
            except Exception:
                vec = None
        try:
            await provider._link_turn_concepts(
                store, embedder, int(row["id"]), sid, content, vec,
            )
            linked += 1
        except Exception:
            skipped += 1
        if linked and linked % 50 == 0:
            print(f"    linked {linked}…", flush=True)
    await pool.close()
    print(f"backfill: linked={linked} skipped={skipped}")
    return linked


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-memory-backfill")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--limit", type=int, default=0, help="0 = all unlinked")
    args = ap.parse_args(argv)
    n = asyncio.run(run_backfill(dsn=args.dsn, limit=args.limit))
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
