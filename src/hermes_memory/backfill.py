"""hermes-memory-backfill-about — leftover AGE Concept/ABOUT linker only.

Does not run live C–F (no nouns, passports, semantic_edge, drain_status).
Manifold backfill is: python scripts/replay_conversation_manifold.py --live
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from .about_concepts import AboutConceptLinker
from .graph_api import classify_session_kind, is_synthetic_session
from .store import Store


def _make_about_linker() -> AboutConceptLinker:
    """Composition: backfill uses the linker without the live provider MRO."""
    linker = AboutConceptLinker()
    linker._concept_names = None
    linker._concept_emb = {}
    return linker


async def run_backfill(dsn: Optional[str] = None, limit: int = 0) -> int:
    import asyncpg

    from .config import load_config
    from .embed import Embedder
    from .provider import _is_noise, should_purge_concept

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
    linker = _make_about_linker()

    pairs = await store.fetch_concept_id_names()
    dead = [vid for vid, name in pairs if should_purge_concept(name)]
    purged = await store.purge_concept_ids(dead)
    if purged:
        print(f"    purged {purged} junk Concept verts", flush=True)
    linker._concept_names = None
    linker._concept_emb = {}

    sql = """
        SELECT c.id, c.session_id, c.content, c.embedding::text AS embedding
          FROM conversations c
         ORDER BY c.id
    """
    async with pool.acquire() as conn:
        sessions = await conn.fetch("SELECT DISTINCT session_id FROM conversations")
        for srow in sessions:
            kind = classify_session_kind(srow["session_id"])
            await conn.execute(
                """
                UPDATE conversations
                   SET metadata = jsonb_set(
                         coalesce(metadata, '{}'::jsonb),
                         '{kind}',
                         to_jsonb($2::text),
                         true
                       )
                 WHERE session_id = $1
                   AND coalesce(metadata->>'kind', '') = ''
                """,
                srow["session_id"],
                kind,
            )
        rows = await conn.fetch(sql)
    linked = 0
    skipped = 0
    for row in rows:
        if limit and linked >= limit:
            break
        sid = row["session_id"] or ""
        if is_synthetic_session(sid):
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
            await linker._link_turn_concepts(
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
    ap = argparse.ArgumentParser(prog="hermes-memory-backfill-about")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--limit", type=int, default=0, help="0 = all unlinked")
    args = ap.parse_args(argv)
    n = asyncio.run(run_backfill(dsn=args.dsn, limit=args.limit))
    return 0 if n >= 0 else 1


def deprecated_main(argv: Optional[list[str]] = None) -> int:
    """Old name. Fail loud so it cannot be mistaken for C–F manifold backfill."""
    del argv
    print(
        "hermes-memory-backfill was renamed: it only writes AGE Concept/ABOUT.\n"
        "  ABOUT leftover:  hermes-memory-backfill-about\n"
        "  live C–F drain:  python scripts/replay_conversation_manifold.py --live\n"
        "Refusing to run under the old name (issue #73).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
