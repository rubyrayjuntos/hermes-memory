"""Reciprocal rank fusion and maximal marginal relevance."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence


def rrf(rankings: Sequence[Sequence[str]], *, k: int = 60) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] += 1.0 / (k + rank)
    return sorted(scores, key=lambda item_id: (-scores[item_id], item_id))


def _cosine(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    if left is None or right is None:
        return 0.0
    dot = sum(x * y for x, y in zip(left, right))
    left_norm = math.sqrt(sum(x * x for x in left))
    right_norm = math.sqrt(sum(y * y for y in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def mmr(
    ids: Sequence[str],
    vectors: dict[str, Sequence[float]],
    *,
    lam: float = 0.7,
    limit: int,
) -> list[str]:
    if not ids or limit <= 0:
        return []
    remaining = list(ids)
    picked = [remaining.pop(0)]
    while remaining and len(picked) < limit:
        best_id = None
        best_score = None
        for item_id in remaining:
            relevance = 1.0 / (1 + ids.index(item_id))
            max_cosine = max(
                _cosine(vectors.get(item_id), vectors.get(picked_id))
                for picked_id in picked
            )
            score = lam * relevance + (1.0 - lam) * (1.0 - max_cosine)
            if best_score is None or score > best_score:
                best_score = score
                best_id = item_id
        remaining.remove(best_id)
        picked.append(best_id)
    return picked
