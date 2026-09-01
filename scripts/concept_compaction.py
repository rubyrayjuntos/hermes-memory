#!/usr/bin/env python3
"""scripts/concept_compaction.py — Concept centroid dedup + orphan prune (issue #37).

- Near-duplicate Concept centroids where embedding cosine >=0.92 -> keep centroid, merge edges, weight = merge count (numeric bare literal)
- Prune isolated Concepts degree==0 older than 7d
- SAVEPOINT-guarded MERGE/DETACH DELETE, no ad-hoc labels (ensure Concept vlabel exists)
- Cosine via real embedding cosine (nomic-embed-text 768d); dry-run preview (pairs, orphans, bridge rows affected)
- Updates bridge table memory_chunk_nodes (loser->keeper, orphan cleanup)

Usage:
    python scripts/concept_compaction.py --dry-run            # preview only
    python scripts/concept_compaction.py --apply               # execute merges + prune
    python scripts/concept_compaction.py --threshold 0.92 --orphan-days 7
    HYBRID_AGE_DSN=postgres://... python scripts/concept_compaction.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return None
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return None
        return dot / (na * nb)
    except Exception:
        return None


def resolve_dsn(cli_dsn: str | None) -> str:
    if cli_dsn:
        return cli_dsn
    env_dsn = os.environ.get("HYBRID_AGE_DSN")
    if env_dsn:
        pw = os.environ.get("HERMES_PG_PASSWORD", "")
        if "***" in env_dsn and pw:
            env_dsn = env_dsn.replace("***", pw)
        if "{pg_password}" in env_dsn and pw:
            env_dsn = env_dsn.replace("{pg_password}", pw)
        return env_dsn
    pw = os.environ.get("HERMES_PG_PASSWORD", "ci-local-password")
    return f"postgres://hermes:{pw}@localhost:5450/hermes_memory"


async def run_compaction(
    dsn: str,
    threshold: float = 0.92,
    orphan_days: int = 7,
    dry_run: bool = True,
) -> Dict:
    import asyncpg
    from hermes_memory.store import Store, compaction_keepers
    from hermes_memory.config import load_config
    from hermes_memory.embed import Embedder

    cfg = load_config()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = Store(pool, graph_name=cfg.graph)
    try:
        # Ensure Concept label exists (no ad-hoc labels)
        await store.ensure_about_labels()

        concepts = await store.fetch_concepts()
        # Embed each concept name via real embedding cosine (768d nomic-embed)
        embedder = None
        try:
            embedder = Embedder(url=cfg.embed_url, model=cfg.embed_model, dim=cfg.embed_dim)
            # quick warmup check
            await embedder.embed_text("warmup")
        except Exception:
            embedder = None

        # Build embedding cache
        emb_cache: Dict[int, List[float] | None] = {}
        for c in concepts:
            name = c.get("name") or ""
            if not name:
                emb_cache[c["id"]] = None
                continue
            vec = None
            if embedder is not None:
                try:
                    vec = await embedder.embed_text(name)
                except Exception:
                    vec = None
            emb_cache[c["id"]] = vec

        # Pairwise cosine >= threshold
        # Deterministic ordering: sort concepts by id ascending before pairing
        concepts_sorted = sorted(concepts, key=lambda x: int(x["id"]))
        raw_pairs: List[Tuple[int, int, float]] = []
        for i in range(len(concepts_sorted)):
            for j in range(i + 1, len(concepts_sorted)):
                a = concepts_sorted[i]
                b = concepts_sorted[j]
                va = emb_cache.get(a["id"])
                vb = emb_cache.get(b["id"])
                cos = cosine_similarity(va, vb)
                # If embedding unavailable, fall back to case-insensitive name equality check
                # but only treat identical lowercased names as near-duplicate (cosine 1.0)
                if cos is None:
                    if (a["name"] or "").strip().lower() == (b["name"] or "").strip().lower() and a["name"]:
                        cos = 1.0
                    else:
                        continue
                cos = max(-1.0, min(1.0, float(cos)))
                if cos >= threshold:
                    # Keep smaller id as keeper candidate (deterministic)
                    keeper, loser = (a["id"], b["id"]) if int(a["id"]) < int(b["id"]) else (b["id"], a["id"])
                    raw_pairs.append((int(keeper), int(loser), float(cos)))

        # Deduplicate overlapping pairs via Union-Find keeper resolution
        # compaction_keepers returns loser->keeper; rebuild pair list deduplicated
        if raw_pairs:
            keeper_map = compaction_keepers(raw_pairs)
            # raw_pairs may have duplicates after transitive grouping; collapse to unique keeper->loser edges
            deduped: List[Tuple[int, int, float]] = []
            # For each loser, find its keeper and keep max cosine among pairs that connect them
            # Build lookup for cosine by unordered pair
            cos_lookup = {}
            for k, l, c in raw_pairs:
                key = tuple(sorted((k, l)))
                if key not in cos_lookup or c > cos_lookup[key]:
                    cos_lookup[key] = c
            for loser, keeper in keeper_map.items():
                key = tuple(sorted((keeper, loser)))
                c = cos_lookup.get(key, threshold)
                # ensure keeper is smaller id (compaction_keepers guarantees)
                deduped.append((int(keeper), int(loser), float(c)))
            # sort deterministic by keeper then loser
            deduped.sort(key=lambda x: (x[0], x[1]))
            pairs = deduped
        else:
            pairs = []

        orphan_ids = await store.find_orphan_concept_ids(days=orphan_days)

        preview = await store.preview_concept_compaction(pairs, orphan_ids)

        if dry_run:
            await pool.close()
            return {
                "dry_run": True,
                "threshold": threshold,
                "orphan_days": orphan_days,
                "concepts_total": len(concepts),
                "pairs": preview["pairs"],
                "orphans": preview["orphans"],
                "bridge_rows_affected": preview["bridge_rows_affected"],
            }

        # Apply: merge pairs sequentially (SAVEPOINT-guarded per pair)
        merged = 0
        for k, l, c in pairs:
            ok = await store.merge_concept_pair(int(k), int(l))
            if ok:
                merged += 1

        pruned = await store.prune_orphan_concepts([int(o) for o in orphan_ids])

        # Re-fetch bridge affected after (should be 0 now for those ids)
        await pool.close()
        return {
            "dry_run": False,
            "threshold": threshold,
            "orphan_days": orphan_days,
            "concepts_total": len(concepts),
            "pairs": preview["pairs"],
            "orphans": preview["orphans"],
            "bridge_rows_affected": preview["bridge_rows_affected"],
            "merged": merged,
            "pruned": pruned,
        }
    except Exception as exc:
        try:
            await pool.close()
        except Exception:
            pass
        raise exc


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Concept centroid compaction (cosine>=0.92) + orphan prune")
    ap.add_argument("--dsn", default=None, help="Postgres DSN (default: HYBRID_AGE_DSN env)")
    ap.add_argument("--threshold", type=float, default=0.92, help="Cosine threshold for near-duplicates")
    ap.add_argument("--orphan-days", type=int, default=7, help="Prune Concepts degree==0 older than N days")
    ap.add_argument("--dry-run", action="store_true", default=True, help="Preview only (default)")
    ap.add_argument("--apply", action="store_true", help="Execute merges + prune (overrides --dry-run)")
    args = ap.parse_args(argv)

    dry_run = not args.apply
    if args.apply:
        dry_run = False
    dsn = resolve_dsn(args.dsn)
    print(f"concept_compaction: threshold={args.threshold} orphan_days={args.orphan_days} dry_run={dry_run}")
    print(f"concept_compaction: dsn={dsn.split('@')[-1] if '@' in dsn else dsn}")
    result = asyncio.run(run_compaction(dsn, threshold=args.threshold, orphan_days=args.orphan_days, dry_run=dry_run))
    import json
    print(json.dumps(result, indent=2))
    if dry_run:
        print(f"dry-run preview: {len(result['pairs'])} pair(s), {len(result['orphans'])} orphan(s), bridge_rows={result['bridge_rows_affected']}")
    else:
        print(f"compaction applied: merged={result.get('merged',0)} pruned={result.get('pruned',0)} bridge_rows={result['bridge_rows_affected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
