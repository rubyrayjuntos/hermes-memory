"""Bounded two-hop claim walk over pre-ordered entity edges."""

from __future__ import annotations

from collections.abc import Sequence


def walk(
    start: Sequence[str],
    edges: dict[str, list[tuple[str, str]]],
    *,
    hops: int = 2,
    per_node: int = 8,
    per_hop: int = 20,
) -> list[str]:
    if hops <= 0 or per_node <= 0 or per_hop <= 0:
        return []

    visited: set[str] = set(start)
    frontier: list[str] = list(start)
    claims: list[str] = []
    seen_claims: set[str] = set()

    for _ in range(hops):
        if not frontier:
            break
        hop_claims = 0
        nxt: list[str] = []
        for entity in frontier:
            if hop_claims >= per_hop:
                break
            outgoing = edges.get(entity, [])[:per_node]
            for claim_id, neighbour in outgoing:
                if hop_claims >= per_hop:
                    break
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                nxt.append(neighbour)
                if claim_id not in seen_claims:
                    seen_claims.add(claim_id)
                    claims.append(claim_id)
                    hop_claims += 1
        frontier = nxt
    return claims
