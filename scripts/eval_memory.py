#!/usr/bin/env python3
"""Quantitative retrieval evaluation for the hybrid-age provider.

Builds a deterministic chat history with known facts, ingests it through the real
provider write path (sync_turn -> drain -> conversations/embeddings/graph), then asks
questions in a new session and measures what ``prefetch`` injects:

- recall: the gold fact turns are present in the injected context
- ablation: gold present in the raw vector top-k (before graph expansion and budget)
- noise: other projects' names injected alongside the answer
- latency: wall-clock per prefetch (p50 / p95)
- ingest: drain throughput (turns/s until every turn is complete)

Synthetic and English-only by design; invented entity names so no built-in taxonomy
can help. Run only against a disposable database (the DSN must name a *_eval* or
hermes_release_gate_* database). Never point it at the installed or dev database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass, field

SYLL = ["quar", "vel", "mir", "tax", "zon", "bren", "kal", "ost", "fen", "gry", "pell", "orin", "shad", "wex", "dur", "lim"]
PEOPLE_FIRST = ["Mara", "Tobin", "Ilse", "Desmond", "Priya", "Okon", "Renata", "Hal", "Sunita", "Anders", "Yuki", "Felix"]
CITIES = ["Lisbon", "Osaka", "Denver", "Tallinn", "Nairobi", "Quebec", "Perth", "Lyon", "Austin", "Seoul"]
CHATTER = [
    "What is a good way to structure a weekly status email for a small team?",
    "Can you explain the difference between a mutex and a semaphore in one paragraph?",
    "Remind me how to format a date string as ISO 8601 in Python.",
    "Summarize the main trade-offs between REST and gRPC for internal services.",
    "I think the build is slow on laptops; what are the usual first things to check?",
]


def _name(rng: random.Random, parts: int = 2) -> str:
    return "".join(rng.choice(SYLL) for _ in range(parts)).capitalize()


@dataclass
class Fact:
    project: str
    owner: str
    city: str
    turns: list[tuple[str, str]] = field(default_factory=list)


def build_corpus(n_facts: int, n_chatter: int, seed: int):
    rng = random.Random(seed)
    facts: list[Fact] = []
    seen: set[str] = set()
    while len(facts) < n_facts:
        project = _name(rng, 3)
        owner = f"{rng.choice(PEOPLE_FIRST)}{_name(rng, 2)}"  # 12 x 256 distinct owners; 1 syllable capped at 192 and hung
        if project in seen or owner in seen:
            continue
        seen.update({project, owner})
        city = rng.choice(CITIES)
        f = Fact(project, owner, city)
        f.turns = [
            (f"Quick update: the {project} project is owned by {owner} and is the team's priority this quarter.",
             f"Noted: {project} belongs to {owner}, who is the owner for planning and budget decisions."),
            (f"Also, {owner} is based in {city} and handles the {project} review calls.",
             f"Understood: {owner} works out of {city} and runs the {project} review calls."),
        ]
        facts.append(f)
    chatter = []
    for i in range(n_chatter):
        q = CHATTER[i % len(CHATTER)]
        # Unique assistant text per turn: identical replies are deduplicated by the store.
        chatter.append((f"{q} (ref {i})", f"Reply {i}: here is a short answer on that topic; " + q[:60] + " is covered in most engineering handbooks."))
    return facts, chatter


def build_queries(facts: list[Fact], n: int, seed: int):
    """Each query: (kind, text, gold_names, gold_turn_indexes). Gold names must appear in injected text."""
    rng = random.Random(seed + 1)
    out = []
    picks = rng.sample(facts, min(n, len(facts)))
    for f in picks:
        kind = rng.choice(["lexical", "owner_lookup", "multihop"])
        if kind == "lexical":
            out.append(("lexical", f"Who owns the {f.project} project?", [f.project, f.owner], [0]))
        elif kind == "owner_lookup":
            out.append(("owner_lookup", f"Which project does {f.owner} lead?", [f.project, f.owner], [0]))
        else:
            out.append(("multihop", f"Where is the owner of {f.project} based?", [f.owner, f.city], [0, 1]))
    return out


def distinct_projects(text: str, all_projects: set[str]) -> set[str]:
    return {p for p in all_projects if re.search(rf"\b{re.escape(p)}\b", text)}


def pct(values, q):
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


async def run(args) -> dict:
    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.provider import HybridAgeMemoryProvider
    from hermes_memory.embed import vec_to_literal
    import asyncpg

    if not re.search(r"(_eval\b|hermes_release_gate_)", args.dsn):
        raise SystemExit("refusing: DSN must name an *_eval or hermes_release_gate_* database")

    cfg = HybridAgeConfig(dsn=args.dsn, prefetch_timeout_s=args.prefetch_timeout)
    prov = HybridAgeMemoryProvider(config=cfg)
    identity = "eval"
    facts, chatter = build_corpus(args.facts, args.chatter, args.seed)
    queries = build_queries(facts, args.queries, args.seed)
    all_projects = {f.project for f in facts}

    prov.initialize(f"eval-ingest-{args.seed}", agent_identity=identity, agent_context="primary")
    t_ingest = time.perf_counter()
    turns = [t for f in facts for t in f.turns] + chatter
    random.Random(args.seed).shuffle(turns)
    # Paced ingest: the write queue holds 256 items and shutdown abandons what is still
    # queued after 5 s, so each chunk is enqueued only after the previous one is persisted.
    conn = await asyncpg.connect(args.dsn)
    deadline = time.monotonic() + args.drain_timeout
    complete = 0
    expected_rows = 0
    for start in range(0, len(turns), args.chunk):
        for user, assistant in turns[start:start + args.chunk]:
            prov.sync_turn(user, assistant, session_id=f"eval-ingest-{args.seed}")
        expected_rows += 2 * len(turns[start:start + args.chunk])
        while time.monotonic() < deadline:
            complete = await conn.fetchval(
                "select count(*) from conversations where drain_status='complete' and agent_identity=$1", identity)
            if complete >= expected_rows:
                break
            await asyncio.sleep(0.5)
    drain_s = time.perf_counter() - t_ingest
    await conn.close()
    prov.shutdown()
    expected = len(turns)

    prov = HybridAgeMemoryProvider(config=cfg)
    prov.initialize(f"eval-query-{args.seed}", agent_identity=identity, agent_context="primary")

    results = {"recall": [], "vector_only_recall": [], "noise": [], "latency_ms": [], "chars": [], "by_kind": {}}
    for kind, text, gold_names, _ in queries:
        t0 = time.perf_counter()
        injected = prov.prefetch(text, session_id=f"eval-query-{args.seed}") or ""
        results["latency_ms"].append((time.perf_counter() - t0) * 1000)
        results["chars"].append(len(injected))
        hit = all(g.lower() in injected.lower() for g in gold_names)
        results["recall"].append(hit)
        results["by_kind"].setdefault(kind, []).append(hit)
        gold_projects = {g for g in gold_names if g in all_projects}
        results["noise"].append(len(distinct_projects(injected, all_projects) - gold_projects))

        # Ablation: raw vector top-k over the same store, before graph expansion and budget.
        # Run on the provider's own loop: its pool/connections are bound to that loop.
        emb = prov._run(prov.embedder.embed_text(text), timeout=args.prefetch_timeout)
        seeds = prov._run(prov.store.vector_search(vec_to_literal(emb), cfg.vector_k, agent_identity=identity),
                          timeout=args.prefetch_timeout)
        raw = " ".join(str(s.get("content") or "") for s in seeds)
        results["vector_only_recall"].append(all(g.lower() in raw.lower() for g in gold_names))
    prov.shutdown()

    def frac(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    summary = {
        "corpus": {"facts": args.facts, "chatter": args.chatter, "turns": expected, "queries": len(queries)},
        "ingest": {"drain_seconds": round(drain_s, 1), "turns_per_s": round(expected / drain_s, 2),
                   "complete_rows": complete, "expected_rows": expected_rows},
        "recall_prefetch": frac(results["recall"]),
        "recall_vector_only": frac(results["vector_only_recall"]),
        "recall_by_kind": {k: frac(v) for k, v in results["by_kind"].items()},
        "noise_projects_per_answer_mean": round(statistics.mean(results["noise"]), 2) if results["noise"] else None,
        "injected_chars_p50": pct(results["chars"], 0.5),
        "latency_ms": {"p50": round(pct(results["latency_ms"], 0.5), 1),
                       "p95": round(pct(results["latency_ms"], 0.95), 1)},
    }
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", required=True)
    p.add_argument("--facts", type=int, default=200)
    p.add_argument("--chatter", type=int, default=800)
    p.add_argument("--queries", type=int, default=150)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--prefetch-timeout", type=float, default=15.0)
    p.add_argument("--drain-timeout", type=float, default=900.0)
    p.add_argument("--chunk", type=int, default=100)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    summary = asyncio.run(run(args))
    print(json.dumps(summary, indent=2) if args.json else json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
