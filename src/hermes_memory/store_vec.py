"""Shared vector helpers for Store and expand_graph (no AGE I/O)."""
from __future__ import annotations

import math
from typing import Any


def _cosine_similarity(a, b):
    """Pure-python cosine: dot/(||a||*||b||). None if invalid."""
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

def _parse_pgvector(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x.strip()]
