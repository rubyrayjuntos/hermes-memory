"""Owner-scoped claim retrieval for the opt-in hybrid read path."""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Any

from .claim_walk import walk
from .entities import normalize_name
from .fusion import mmr, rrf
from .store_vec import _parse_pgvector

_STOPWORDS = {
    "a", "an", "and", "are", "does", "for", "how", "is", "of", "the", "to",
    "was", "were", "what", "when", "where", "which", "who", "why", "with",
}


def _entity_phrases(query: str) -> list[str]:
    words = re.findall(r"[\w'-]+", normalize_name(query))
    phrases: list[str] = []
    for size in (1, 2, 3):
        for start in range(max(0, len(words) - size + 1)):
            part = words[start:start + size]
            if part[0] in _STOPWORDS or part[-1] in _STOPWORDS:
                continue
            phrase = " ".join(part)
            if phrase not in phrases and not all(word in _STOPWORDS for word in part):
                phrases.append(phrase)
    return phrases


async def retrieve_hybrid(
    store: Any,
    owner: str,
    query: str,
    vec_literal: str,
    vector_seeds: list[dict],
    *,
    limit: int = 24,
) -> dict:
    """Run bounded claim ANN, batched entity linking, two-hop walk, RRF and MMR."""
    started = time.perf_counter()
    turn_ids: list[int] = []
    for seed in vector_seeds:
        if seed.get("src") != "conversation":
            continue
        raw_id = str(seed.get("id") or "").removeprefix("conv_")
        if raw_id.isdigit():
            turn_ids.append(int(raw_id))

    phrases = _entity_phrases(query)
    t0 = time.perf_counter()
    claim_hits, linked, turn_entities = await asyncio.gather(
        store.claim_vector_search(owner, vec_literal, limit),
        store.link_entities(owner, phrases, vec_literal),
        store.turn_entities(owner, turn_ids),
    )
    link_ms = (time.perf_counter() - t0) * 1000

    turn_claim_ids = await store.claims_for_turns(owner, turn_ids)
    turn_entity_ids = [int(row["entity_id"]) for row in turn_entities]
    linked_entity_ids = [int(row["entity_id"]) for row in linked]
    start_entities = list(dict.fromkeys(linked_entity_ids + turn_entity_ids))
    for hit in claim_hits:
        for key in ("subject_id", "object_id"):
            if hit.get(key) is not None:
                start_entities.append(int(hit[key]))
    start_entities = list(dict.fromkeys(start_entities))

    edge_rows: list[dict] = []
    frontier = start_entities
    visited = set(start_entities)
    t0 = time.perf_counter()
    for _ in range(2):
        if not frontier:
            break
        rows = await store.claim_edges(owner, frontier, vec_literal, per_node=8)
        hop_rows = rows[:20]
        edge_rows.extend(hop_rows)
        next_frontier = []
        for row in hop_rows:
            neighbour = int(row["neighbour_id"])
            if neighbour not in visited:
                visited.add(neighbour)
                next_frontier.append(neighbour)
        frontier = next_frontier
    walk_ms = (time.perf_counter() - t0) * 1000

    edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in edge_rows:
        edges[str(row["source_id"])].append(
            (str(row["claim_id"]), str(row["neighbour_id"]))
        )
    walked_claim_ids = walk(
        [str(entity_id) for entity_id in start_entities],
        dict(edges),
        hops=2,
        per_node=8,
        per_hop=20,
    )
    claim_rank = [str(row["claim_id"]) for row in claim_hits]
    rankings = [
        claim_rank,
        list(dict.fromkeys(turn_claim_ids)),
        walked_claim_ids,
    ]
    fused_ids = rrf(rankings, k=60)
    candidate_ids = list(dict.fromkeys(
        fused_ids + claim_rank + turn_claim_ids + [str(row["claim_id"]) for row in edge_rows]
    ))
    candidate_rows = await store.claims_by_ids(owner, candidate_ids)

    grouped: dict[str, dict] = {}
    for row in candidate_rows:
        claim_id = str(row["claim_id"])
        fact_text = str(row.get("fact_text") or "").strip()
        if not fact_text:
            fact_text = " ".join(
                str(row.get(key) or "").replace("_", " ")
                for key in ("subject", "verb", "object")
            ).strip()
        item = grouped.setdefault(claim_id, {
            "claim_id": claim_id,
            "status": row.get("status"),
            "fact_text": fact_text,
            "fact_vec": row.get("fact_vec"),
            "supporting_turns": [],
        })
        if row.get("span_id") is not None:
            item["supporting_turns"].append({
                "span_id": int(row["span_id"]),
                "event_time": row.get("event_time"),
                "role": row.get("role"),
                "content": row.get("content") or "",
                "ts": row.get("ts"),
                "session_id": row.get("session_id"),
            })

    # MMR uses each current claim's own persisted fact vector.
    vectors = {
        claim_id: _parse_pgvector(item.get("fact_vec"))
        for claim_id, item in grouped.items()
    }
    diversified = mmr(
        [claim_id for claim_id in fused_ids if claim_id in grouped],
        vectors,
        lam=0.7,
        limit=min(12, max(0, int(limit))),
    )
    facts = []
    for claim_id in diversified:
        item = grouped[claim_id]
        unique_turns = {
            int(turn["span_id"]): turn
            for turn in item["supporting_turns"]
        }
        facts.append({
            "claim_id": claim_id,
            "status": item["status"],
            "fact_text": item["fact_text"],
            "event_time": max(
                (turn.get("event_time") for turn in unique_turns.values()),
                default=None,
            ),
            "supporting_turns": list(unique_turns.values()),
        })

    return {
        "facts": facts,
        "ids": [fact["claim_id"] for fact in facts],
        "latency_ms": {
            "claim_link": round(link_ms, 1),
            "walk": round(walk_ms, 1),
            "total": round((time.perf_counter() - started) * 1000, 1),
        },
    }
