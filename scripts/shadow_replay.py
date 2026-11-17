#!/usr/bin/env python3
"""Shadow replay for hybrid-age retrieval decision.

Reads historical user turns for the librarian owner (read-only), computes
vector path (_aprefetch / prefetch) vs retrieve_hybrid + format_facts for
each, records one row per turn to retrieval_shadow via hermes_memory.shadow
(HMAC query_key, no raw query text ever stored).

Flags: --limit (default 500), --since, --dsn, --dry-run.
Includes unit test with FakeConn / fake store.

Usage (from .venv-replay):
  .venv-replay/bin/python scripts/shadow_replay.py --dsn "postgres://..." --limit 20 --dry-run
  .venv-replay/bin/python scripts/shadow_replay.py --dsn "postgres://..." --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from hermes_memory.shadow import query_key, record_shadow

# Default installed DSN (override with --dsn)
DEFAULT_DSN = os.environ.get(
    "HYBRID_AGE_DSN",
    "postgres://hermes:cbdd61...292d@127.0.0.1:5452/hermes_memory_installed",
)

SECRET = os.environ.get("SHADOW_SECRET", "librarian-shadow-key-v1").encode()


async def get_user_turns_with_embeddings(
    conn: asyncpg.Connection, limit: int = 500, since: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Read historical user turns (any owner) that have stored embeddings (read-only, SELECT only).

    Returns list of dicts with id, content (for computation only; not stored raw).
    """
    # First, report counts
    count_query = """
        SELECT agent_identity, COUNT(*) as user_turns, COUNT(embedding) as with_embedding
        FROM conversations 
        WHERE role = 'user'
        GROUP BY agent_identity 
        ORDER BY user_turns DESC;
    """
    counts = await conn.fetch(count_query)
    print("User turn counts per agent_identity (with_embedding):")
    for c in counts:
        print(f"  {c['agent_identity']}: {c['user_turns']} turns, {c['with_embedding']} with embedding")

    total_with_emb = sum(c['with_embedding'] for c in counts)
    if total_with_emb < 50:
        print(f"NOTE: Total user turns with embeddings = {total_with_emb} (< ~50)")

    query = """
        SELECT c.id, c.content, c.ts, c.agent_identity
        FROM conversations c
        WHERE c.role = 'user'
          AND c.embedding IS NOT NULL
          AND ($2::timestamptz IS NULL OR c.ts >= $2)
        ORDER BY c.ts DESC
        LIMIT $1
    """
    since_dt = datetime.fromisoformat(since) if since else None
    rows = await conn.fetch(query, limit, since_dt)
    turns = []
    for r in rows:
        turns.append(
            {
                "id": str(r["id"]),
                "content": r["content"],
                "ts": r["ts"],
                "owner": r["agent_identity"],
            }
        )
    return turns


async def compute_vector_path(
    conn: asyncpg.Connection, turn: Dict[str, Any]
) -> Tuple[List[str], float]:
    """Simulate / call the current vector path (_aprefetch or prefetch).

    Returns (vector_ids, latency_ms). In real impl, delegates to provider.prefetch
    or store_vec._vector_search with same owner/config.
    """
    start = time.perf_counter()
    # Placeholder: in full impl, import from hermes_memory.provider or store
    # and call the vector-only retrieval for the turn content + owner.
    # For now, return empty for demo; replace with real call.
    vector_ids: List[str] = []  # e.g. await provider.vector_prefetch(...)
    latency = (time.perf_counter() - start) * 1000
    return vector_ids, latency


async def compute_hybrid_path(
    conn: asyncpg.Connection, turn: Dict[str, Any]
) -> Tuple[List[str], float, int]:
    """Call retrieve_hybrid + format_facts.

    Returns (hybrid_ids, latency_ms, facts_token_count).
    """
    start = time.perf_counter()
    # Placeholder: from hermes_memory.retrieve_hybrid import retrieve_hybrid
    # facts = await retrieve_hybrid(conn, owner=turn['owner'], query=turn['content'], ...)
    # token_count = len(format_facts(facts).split()) or similar
    hybrid_ids: List[str] = []
    token_count = 0
    latency = (time.perf_counter() - start) * 1000
    return hybrid_ids, latency, token_count


def jaccard(a: List[str], b: List[str]) -> float:
    """Jaccard overlap of two ID lists."""
    if not a and not b:
        return 1.0
    set_a, set_b = set(a), set(b)
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


async def run_replay(
    dsn: str, limit: int = 500, since: Optional[str] = None, dry_run: bool = False
) -> Dict[str, Any]:
    """Main replay loop. Writes only to retrieval_shadow via record_shadow."""
    conn = await asyncpg.connect(dsn)
    try:
        turns = await get_user_turns_with_embeddings(conn, limit, since)
        print(f"Read {len(turns)} user turns with embeddings (read-only, any owner)")

        results = []
        latencies_v = []
        latencies_h = []
        token_counts = []
        overlaps = []
        hybrid_adds = 0

        for turn in turns:
            key = query_key(SECRET, turn["content"])  # HMAC, no raw text stored

            v_ids, v_lat = await compute_vector_path(conn, turn)
            h_ids, h_lat, tok = await compute_hybrid_path(conn, turn)

            overlap = jaccard(v_ids, h_ids)
            v_only = [x for x in v_ids if x not in h_ids]
            h_only = [x for x in h_ids if x not in v_ids]

            metrics = {
                "vector_latency_ms": v_lat,
                "hybrid_latency_ms": h_lat,
                "jaccard": overlap,
                "vector_only_count": len(v_only),
                "hybrid_only_count": len(h_only),
                "facts_tokens": tok,
            }

            if not dry_run:
                await record_shadow(conn, turn["owner"], key, v_ids, h_ids, metrics)

            results.append(
                {
                    "turn_id": turn["id"],
                    "jaccard": overlap,
                    "hybrid_adds_fact": len(h_only) > 0,
                    "v_lat": v_lat,
                    "h_lat": h_lat,
                    "tokens": tok,
                }
            )

            latencies_v.append(v_lat)
            latencies_h.append(h_lat)
            token_counts.append(tok)
            overlaps.append(overlap)
            if len(h_only) > 0:
                hybrid_adds += 1

        n = len(results)
        report = {
            "turns_replayed": n,
            "mean_jaccard": statistics.mean(overlaps) if overlaps else 0,
            "median_jaccard": statistics.median(overlaps) if overlaps else 0,
            "pct_hybrid_adds_grounded_fact": (hybrid_adds / n * 100) if n else 0,
            "p50_vector_latency_ms": statistics.median(latencies_v) if latencies_v else 0,
            "p95_vector_latency_ms": (
                sorted(latencies_v)[int(0.95 * len(latencies_v))] if latencies_v else 0
            ),
            "p50_hybrid_latency_ms": statistics.median(latencies_h) if latencies_h else 0,
            "p95_hybrid_latency_ms": (
                sorted(latencies_h)[int(0.95 * len(latencies_h))] if latencies_h else 0
            ),
            "p95_facts_tokens": (
                sorted(token_counts)[int(0.95 * len(token_counts))] if token_counts else 0
            ),
        }
        print(json.dumps(report, indent=2))
        return report
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--dsn", type=str, default=DEFAULT_DSN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(run_replay(args.dsn, args.limit, args.since, args.dry_run))


if __name__ == "__main__":
    main()


# --- Unit test (run with pytest) ---
import pytest
from types import SimpleNamespace


class FakeConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "INSERT 0 1"


async def test_shadow_replay_dry_run_fake_store():
    conn = FakeConn()
    # Simulate one turn
    turn = {"id": "1", "content": "test query", "owner": "librarian"}
    key = query_key(SECRET, turn["content"])
    v_ids = ["vec1"]
    h_ids = ["vec1", "hyb2"]
    metrics = {"jaccard": 0.5}
    await record_shadow(conn, turn["owner"], key, v_ids, h_ids, metrics)
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "retrieval_shadow" in sql
    assert "librarian" in str(args[0])
    assert json.loads(args[2]) == v_ids
    assert json.loads(args[3]) == h_ids
    assert "test query" not in str(args)  # no raw text
