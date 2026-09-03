"""Pure scoring and row types for the bounded conversation manifold walk."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class WalkHypothesis:
    noun_id: int
    chunk_id: str
    session_id: str
    turn_id: int
    sim: float
    chunk_vec: list[float]


class WalkRow(tuple):
    """A walk tuple with audit metadata carried beside its public slots."""

    audit: dict[str, Any]

    def __new__(
        cls,
        values: tuple[Any, ...],
        *,
        audit: dict[str, Any],
    ) -> "WalkRow":
        row = super().__new__(cls, values)
        row.audit = audit
        return row


def clamp_cos(x: float) -> float:
    return max(0.0, float(x))


def provenance_boost(turn_id: int, provenance_turns: Sequence[int]) -> float:
    return 1.0 if int(turn_id) in provenance_turns else 0.0


def consensus_decay(
    *,
    empty_incident: bool,
    local_dir: float,
    query_dir: float,
    hop: int,
    lam: float = 0.05,
) -> float:
    if empty_incident:
        return 1.0
    return math.exp(
        -float(lam) * int(hop) * (1.0 - float(local_dir) * float(query_dir))
    )


def beam_score(
    *,
    sim: float,
    src_align: float,
    tgt_align: float,
    prov_boost: float,
    decay: float,
    magnitude: float,
) -> tuple[float, float, float]:
    c = float(src_align) * float(tgt_align)
    composite = (
        0.4 * float(sim)
        + 0.4 * c * float(prov_boost)
        + 0.2 * float(decay)
    )
    return c, composite, composite * (float(magnitude) / 8.0)
