#!/usr/bin/env python3
"""backtest_inferred_edges.py — offline, read-only lab for similarity-inferred
"virtual" edges. Does NOT write to the database and does NOT touch the live
write or recall path.

Historical result and the defer decision:
https://github.com/rubyrayjuntos/hermes-memory/wiki/Experiment-Similarity-Inferred-Edges

The published run used hermes_memory on :5450 (not hermes_test, not
hermes_memory_installed), full nouns at cutoff, and --control-sample 8000.

    python3 scripts/lab/backtest_inferred_edges.py --cutoff-fraction 0.4 \
        --max-nouns 2500 --control-sample 8000
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sys
from urllib.parse import urlparse

import asyncpg

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "src"))

from hermes_memory.config import load_config  # noqa: E402
from hermes_memory.embed import Embedder  # noqa: E402


def cosine(a: list[float], b: list[float]) -> float:
    denom = (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) + 1e-9
    return float(sum(x * y for x, y in zip(a, b)) / denom)


def resolve_dsn(cfg) -> str:
    dsn = os.environ.get("HYBRID_AGE_DSN") or cfg.dsn
    if "{pg_password}" in dsn:
        dsn = dsn.replace("{pg_password}", os.environ.get("HERMES_PG_PASSWORD", ""))
    if "***" in dsn:
        dsn = dsn.replace("***", os.environ.get("HERMES_PG_PASSWORD", ""))
    return dsn


def dsn_ok(dsn: str) -> bool:
    """Allow the historical DB this experiment used; refuse the installed pin."""
    parsed = urlparse(dsn)
    db = (parsed.path or "/").rsplit("/", 1)[-1]
    return db == "hermes_memory"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cutoff-fraction", type=float, default=0.6,
                     help="Fraction of turn-id range treated as known so far.")
    ap.add_argument("--similarity-threshold", type=float, default=0.80,
                     help="Cosine similarity above which a pair becomes a candidate.")
    ap.add_argument("--max-nouns", type=int, default=2500,
                     help="Cap nouns considered. 2500 is above the live noun count.")
    ap.add_argument("--control-sample", type=int, default=8000,
                     help="Random unconnected pairs for the control. Independent of candidate count.")
    args = ap.parse_args()

    cfg = load_config()
    dsn = resolve_dsn(cfg)
    if not dsn_ok(dsn):
        print(
            "Refusing: point HYBRID_AGE_DSN at hermes_memory (dev :5450), "
            "not hermes_memory_installed or another database.",
            file=sys.stderr,
        )
        return 2

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    embedder = Embedder(cfg.embed_url, cfg.embed_model, cfg.embed_dim)

    async with pool.acquire() as conn:
        noun_rows = await conn.fetch(
            """
            SELECT n.id, n.label, MIN(m.turn_id) AS first_turn
              FROM noun n
              JOIN memory_chunk_nodes m ON m.noun_id = n.id AND m.source = 'conversation'
             GROUP BY n.id, n.label
            """
        )
        edge_rows = await conn.fetch(
            """
            SELECT src_noun, tgt_noun,
                   (SELECT MIN(t) FROM unnest(provenance_turns) AS t) AS first_turn
              FROM semantic_edge
             WHERE verb_type = 'mentions'
            """
        )
    await pool.close()

    if not noun_rows or not edge_rows:
        print("Not enough data in this database to backtest.", file=sys.stderr)
        return 1

    turn_ids = [r["first_turn"] for r in noun_rows if r["first_turn"] is not None]
    lo, hi = min(turn_ids), max(turn_ids)
    cutoff = lo + (hi - lo) * args.cutoff_fraction
    print(f"turn id range [{lo}, {hi}], cutoff={cutoff:.0f} "
          f"({args.cutoff_fraction:.0%} through history)")

    known_nouns = [r for r in noun_rows if r["first_turn"] is not None and r["first_turn"] <= cutoff]
    if len(known_nouns) > args.max_nouns:
        known_nouns = random.sample(known_nouns, args.max_nouns)
    print(f"nouns known as of cutoff: {len(known_nouns)} "
          f"(of {len(noun_rows)} total, capped at {args.max_nouns})")

    connected_as_of_cutoff: set[tuple[int, int]] = set()
    connected_ever: set[tuple[int, int]] = set()
    for e in edge_rows:
        pair = tuple(sorted((int(e["src_noun"]), int(e["tgt_noun"]))))
        connected_ever.add(pair)
        if e["first_turn"] is not None and e["first_turn"] <= cutoff:
            connected_as_of_cutoff.add(pair)

    labels = [r["label"] for r in known_nouns]
    print(f"embedding {len(labels)} noun labels...")
    vectors = await embedder.embed_texts(labels)
    id_vec = {
        int(known_nouns[i]["id"]): v
        for i, v in enumerate(vectors) if v
    }
    ids = list(id_vec.keys())
    print(f"embedded {len(ids)} (some may have failed and were skipped)")

    candidates: list[tuple[int, int, float]] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            pair = tuple(sorted((a, b)))
            if pair in connected_as_of_cutoff:
                continue
            sim = cosine(id_vec[a], id_vec[b])
            if sim >= args.similarity_threshold:
                candidates.append((a, b, sim))

    confirmed = sum(1 for a, b, _ in candidates if tuple(sorted((a, b))) in connected_ever)
    precision = confirmed / len(candidates) if candidates else 0.0

    all_unconnected = [
        (ids[i], ids[j])
        for i in range(len(ids)) for j in range(i + 1, len(ids))
        if tuple(sorted((ids[i], ids[j]))) not in connected_as_of_cutoff
    ]
    n_control = min(args.control_sample, len(all_unconnected))
    control_sample = random.sample(all_unconnected, n_control)
    control_confirmed = sum(
        1 for a, b in control_sample if tuple(sorted((a, b))) in connected_ever
    )
    control_rate = control_confirmed / len(control_sample) if control_sample else 0.0

    print()
    print("=== Result ===")
    print(f"candidates proposed (sim >= {args.similarity_threshold}): {len(candidates)}")
    print(f"  later confirmed by a real edge: {confirmed}  (precision={precision:.2%})")
    print(f"unconnected pairs available: {len(all_unconnected)}")
    print(f"random-pair control (target {args.control_sample}): {len(control_sample)}")
    print(f"  confirmed by chance: {control_confirmed}  (base rate={control_rate:.2%})")
    print()
    print("Print rates only. Do not treat 1.5× a zero control as a verdict; "
          "see the wiki experiment page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
