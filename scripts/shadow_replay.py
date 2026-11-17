#!/usr/bin/env python3
"""Offline vector vs hybrid shadow replay.

Builds a real Store/pool the same way HybridAgeMemoryProvider._ainit constructs
them, loads the provider embedder, and for each historical user turn runs:

  (a) the provider's vector-mode _aprefetch
  (b) retrieve_hybrid + format_facts with the same owner/config

Writes retrieval_shadow via hermes_memory.shadow.record_shadow (HMAC query_key,
never the raw query) unless --dry-run.

Usage:
  .venv-replay/bin/python scripts/shadow_replay.py --dsn "$DSN" --limit 20 --dry-run
  .venv-replay/bin/python scripts/shadow_replay.py --dsn "$DSN" --limit 500 --json-out out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import traceback
from datetime import datetime
from typing import Any

ERROR_RATE_MAX = 0.10

from hermes_memory.config import HybridAgeConfig, load_config
from hermes_memory.embed import Embedder, vec_to_literal
from hermes_memory.provider import SECRET_RE, HybridAgeMemoryProvider
from hermes_memory.provider_helpers import format_facts
from hermes_memory.retrieve_hybrid import retrieve_hybrid
from hermes_memory.schema_guard import apply_pending_migrations
from hermes_memory.shadow import query_key, record_shadow
from hermes_memory.store import Store
from hermes_memory.tokens import QUERY_TOKEN_CAP, count_tokens, injection_token_cap, truncate_tokens


def resolve_dsn(cli_dsn: str | None) -> str:
    if cli_dsn:
        os.environ["HYBRID_AGE_DSN"] = cli_dsn
    cfg = load_config()
    dsn = cfg.dsn
    pw = os.environ.get("HERMES_PG_PASSWORD", "")
    if pw:
        dsn = dsn.replace("{pg_password}", pw).replace("***", pw)
    return dsn


def load_hmac_secret(*, required: bool) -> bytes:
    secret_hex = os.environ.get("HYBRID_AGE_SHADOW_HMAC_KEY", "")
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        secret = b""
    if len(secret) < 32:
        if required:
            raise SystemExit("HYBRID_AGE_SHADOW_HMAC_KEY is missing or invalid (need >=32 bytes hex)")
        return b"\x00" * 32
    return secret


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def facts_block_and_total_tokens(block: str) -> tuple[int, int]:
    text = block or ""
    fact_block = text.split("<supporting_turns>", 1)[0].strip()
    return count_tokens(fact_block), count_tokens(text)


def jaccard(a: list[str], b: list[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 1.0
    union = len(set_a | set_b)
    return (len(set_a & set_b) / union) if union else 0.0


async def fetch_user_turns(
    conn: Any,
    *,
    limit: int,
    since: str | None,
    owner: str | None,
) -> list[dict[str, Any]]:
    counts = await conn.fetch(
        """
        SELECT agent_identity, COUNT(*) AS user_turns
          FROM conversations
         WHERE role = 'user'
         GROUP BY agent_identity
         ORDER BY user_turns DESC
        """
    )
    print("User turn counts per agent_identity:")
    for row in counts:
        print(f"  {row['agent_identity']}: {row['user_turns']}")

    since_dt = datetime.fromisoformat(since) if since else None
    rows = await conn.fetch(
        """
        SELECT id, content, ts, agent_identity
          FROM conversations
         WHERE role = 'user'
           AND ($3::text IS NULL OR agent_identity = $3)
           AND ($2::timestamptz IS NULL OR ts >= $2)
         ORDER BY ts DESC
         LIMIT $1
        """,
        limit,
        since_dt,
        owner,
    )
    return [
        {
            "id": str(row["id"]),
            "content": row["content"] or "",
            "ts": row["ts"],
            "owner": row["agent_identity"],
        }
        for row in rows
    ]


async def build_provider(
    cfg: HybridAgeConfig,
    embedder: Any | None = None,
) -> HybridAgeMemoryProvider:
    """Store/pool construction copied from HybridAgeMemoryProvider._ainit.

    Background drain / outbox / extraction tasks are not started: this script
    is SELECT on conversations plus optional retrieval_shadow inserts.
    """
    import asyncpg

    provider = HybridAgeMemoryProvider(config=cfg)
    provider.embedder = embedder or Embedder(
        url=cfg.embed_url,
        model=cfg.embed_model,
        dim=cfg.embed_dim,
    )
    dsn = cfg.dsn
    if "{pg_password}" in dsn or "***" in dsn:
        pw = os.environ.get("HERMES_PG_PASSWORD", "")
        dsn = dsn.replace("{pg_password}", pw).replace("***", pw)
    await asyncio.to_thread(apply_pending_migrations, dsn)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    provider.pool = pool
    provider.store = Store(
        pool,
        graph_name=cfg.graph,
        embed_model=cfg.embed_model,
        embed_dim=cfg.embed_dim,
        hnsw_ef_search=int(getattr(cfg, "hnsw_ef_search", 100)),
    )
    await provider.store.require_schema_head()
    provider._shadow_hook = None
    provider._shadow_enabled = False
    return provider


def _capture_budget(provider: HybridAgeMemoryProvider, holder: dict[str, list[str]]):
    original = provider._budget_seeds

    def wrapped(seeds: list[dict]) -> list[dict]:
        selected = original(seeds)
        holder["ids"] = [str(seed.get("id")) for seed in selected if seed.get("id") is not None]
        holder["spans"] = [
            str(seed["turn_id"]) for seed in selected if seed.get("turn_id") is not None
        ]
        return selected

    return wrapped


async def compute_vector_path(
    provider: HybridAgeMemoryProvider,
    query: str,
    owner: str,
) -> dict[str, Any]:
    """Provider vector-mode prefetch. Exceptions propagate."""
    if provider.embedder is None or provider.store is None:
        raise RuntimeError("provider is missing embedder or store")
    previous_mode = provider.config.retrieval_mode
    previous_owner = provider._agent_identity
    captured: dict[str, list[str]] = {"ids": [], "spans": []}
    original_budget = provider._budget_seeds
    provider.config.retrieval_mode = "vector"
    provider._agent_identity = owner
    provider._budget_seeds = _capture_budget(provider, captured)
    started = time.perf_counter()
    try:
        block = await provider._aprefetch(query)
    finally:
        latency_ms = (time.perf_counter() - started) * 1000
        provider.config.retrieval_mode = previous_mode
        provider._agent_identity = previous_owner
        provider._budget_seeds = original_budget
    block = block or ""
    return {
        "ids": list(captured["ids"]),
        "spans": list(captured["spans"]),
        "latency_ms": latency_ms,
        "tokens": count_tokens(block),
        "block": block,
    }


async def compute_hybrid_path(
    provider: HybridAgeMemoryProvider,
    query: str,
    owner: str,
) -> dict[str, Any]:
    """retrieve_hybrid + format_facts. Exceptions propagate."""
    if provider.embedder is None or provider.store is None:
        raise RuntimeError("provider is missing embedder or store")
    started = time.perf_counter()
    enriched = provider._enrich_query(query)
    emb = await provider.embedder.embed_text(
        truncate_tokens(enriched, QUERY_TOKEN_CAP), as_query=True
    )
    if not emb:
        raise RuntimeError("embedder returned no vector")
    vec_literal = vec_to_literal(emb)
    seeds = await provider.store.vector_search(
        vec_literal,
        provider.config.vector_k,
        agent_identity=owner,
    )
    kept = []
    for seed in seeds:
        if seed["similarity"] < provider.config.min_similarity:
            continue
        if SECRET_RE.search(seed["content"] or ""):
            continue
        kept.append(seed)
    result = await retrieve_hybrid(provider.store, owner, query, vec_literal, kept)
    block = format_facts(
        result["facts"],
        total_token_budget=injection_token_cap(provider.config.max_tokens),
    )
    latency_ms = (time.perf_counter() - started) * 1000
    spans: list[str] = []
    seen: set[str] = set()
    for fact in result["facts"]:
        for turn in fact.get("supporting_turns") or []:
            if turn.get("span_id") is None:
                continue
            span = str(turn["span_id"])
            if span not in seen:
                seen.add(span)
                spans.append(span)
    fact_tokens, total_tokens = facts_block_and_total_tokens(block)
    return {
        "ids": [str(item) for item in result["ids"]],
        "spans": spans,
        "latency_ms": latency_ms,
        "tokens": total_tokens,
        "facts_tokens": fact_tokens,
        "total_tokens": total_tokens,
        "block": block or "",
        "arm_latency_ms": dict(result.get("latency_ms") or {}),
    }


async def replay_turns(
    provider: HybridAgeMemoryProvider,
    turns: list[dict[str, Any]],
    *,
    secret: bytes,
    dry_run: bool,
) -> dict[str, Any]:
    pool = provider.pool
    if pool is None:
        raise RuntimeError("provider has no pool")
    vector_errors = 0
    hybrid_errors = 0
    first_traceback: str | None = None
    rows: list[dict[str, Any]] = []
    v_latencies: list[float] = []
    h_latencies: list[float] = []
    facts_token_counts: list[int] = []
    total_token_counts: list[int] = []
    overlaps: list[float] = []
    hybrid_adds = 0

    async with pool.acquire() as conn:
        for turn in turns:
            query = turn["content"]
            owner = turn["owner"]
            vector: dict[str, Any] | None = None
            hybrid: dict[str, Any] | None = None
            try:
                vector = await compute_vector_path(provider, query, owner)
            except Exception:
                vector_errors += 1
                if first_traceback is None:
                    first_traceback = traceback.format_exc()
                    print(first_traceback, file=sys.stderr)
            try:
                hybrid = await compute_hybrid_path(provider, query, owner)
            except Exception:
                hybrid_errors += 1
                if first_traceback is None:
                    first_traceback = traceback.format_exc()
                    print(first_traceback, file=sys.stderr)
            if vector is None or hybrid is None:
                continue

            overlap = jaccard(vector["spans"], hybrid["spans"])
            hybrid_only = [span for span in hybrid["spans"] if span not in set(vector["spans"])]
            metrics = {
                "vector": round(vector["latency_ms"], 1),
                "hybrid": round(hybrid["latency_ms"], 1),
                "vector_tokens": vector["tokens"],
                "hybrid_tokens": hybrid["total_tokens"],
                "hybrid_facts_tokens": hybrid["facts_tokens"],
                "hybrid_total_tokens": hybrid["total_tokens"],
                "jaccard_spans": overlap,
                **{f"hybrid_{key}": value for key, value in hybrid["arm_latency_ms"].items()},
            }
            if not dry_run:
                await record_shadow(
                    conn,
                    owner,
                    query_key(secret, query),
                    vector["ids"],
                    hybrid["ids"],
                    metrics,
                )
            v_latencies.append(vector["latency_ms"])
            h_latencies.append(hybrid["latency_ms"])
            facts_token_counts.append(hybrid["facts_tokens"])
            total_token_counts.append(hybrid["total_tokens"])
            overlaps.append(overlap)
            if hybrid_only:
                hybrid_adds += 1
            rows.append(
                {
                    "turn_id": turn["id"],
                    "owner": owner,
                    "jaccard_spans": overlap,
                    "hybrid_adds_span": bool(hybrid_only),
                    "vector_ids": vector["ids"],
                    "hybrid_ids": hybrid["ids"],
                    "vector_latency_ms": vector["latency_ms"],
                    "hybrid_latency_ms": hybrid["latency_ms"],
                    "vector_tokens": vector["tokens"],
                    "hybrid_tokens": hybrid["tokens"],
                }
            )

    n = len(turns)
    compared = len(rows)
    report = {
        "turns_read": n,
        "turns_compared": compared,
        "vector_errors": vector_errors,
        "hybrid_errors": hybrid_errors,
        "vector_error_rate": (vector_errors / n) if n else 0.0,
        "hybrid_error_rate": (hybrid_errors / n) if n else 0.0,
        "mean_jaccard_spans": statistics.mean(overlaps) if overlaps else None,
        "median_jaccard_spans": statistics.median(overlaps) if overlaps else None,
        "pct_hybrid_adds_span": (hybrid_adds / compared * 100) if compared else 0.0,
        "p50_vector_latency_ms": percentile(v_latencies, 0.5),
        "p95_vector_latency_ms": percentile(v_latencies, 0.95),
        "p50_hybrid_latency_ms": percentile(h_latencies, 0.5),
        "p95_hybrid_latency_ms": percentile(h_latencies, 0.95),
        "p95_facts_tokens": percentile([float(v) for v in facts_token_counts], 0.95),
        "p95_total_tokens": percentile([float(v) for v in total_token_counts], 0.95),
        "dry_run": dry_run,
    }
    return report


async def run(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    os.environ["HYBRID_AGE_DSN"] = dsn
    cfg = load_config()
    cfg.dsn = dsn
    # Instance-only timeout so _aprefetch does not return "" on a 2s cap.
    cfg.prefetch_timeout_s = max(float(cfg.prefetch_timeout_s), 30.0)
    secret = load_hmac_secret(required=not args.dry_run)
    provider = await build_provider(cfg)
    try:
        pool = provider.pool
        if pool is None:
            raise RuntimeError("provider has no pool")
        async with pool.acquire() as conn:
            turns = await fetch_user_turns(
                conn, limit=args.limit, since=args.since, owner=args.owner
            )
        print(f"Read {len(turns)} user turns (SELECT only)")
        report = await replay_turns(
            provider, turns, secret=secret, dry_run=args.dry_run
        )
    finally:
        if provider.pool is not None:
            await provider.pool.close()

    payload = json.dumps(report, indent=2, default=str)
    print(payload)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")

    n = report["turns_read"]
    if n and (
        report["vector_error_rate"] > ERROR_RATE_MAX
        or report["hybrid_error_rate"] > ERROR_RATE_MAX
    ):
        print(
            f"error rate exceeded {ERROR_RATE_MAX:.0%}: "
            f"vector={report['vector_error_rate']:.1%} hybrid={report['hybrid_error_rate']:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--since", default=None)
    parser.add_argument("--owner", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
