"""Conversation-manifold expand_graph — extracted from Store."""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

from .store_vec import _cosine_similarity, _parse_pgvector
from .walk import (
    WalkHypothesis,
    WalkRow,
    beam_score,
    clamp_cos,
    consensus_decay,
    provenance_boost,
)


class StoreExpandMixin:
    async def expand_graph(
        self,
        hypotheses: Sequence[WalkHypothesis],
        *,
        q_vec: list[float],
        hops: int = 2,
        k: int = 8,
    ) -> List[Tuple[Any, Optional[str], Any, float, float, float, float]]:
        """Walk outgoing ``mentions`` poles for at most two bounded hops.

        Each returned value is a seven-slot tuple. ``WalkRow.audit`` carries
        the parent passport's session, turn, chunk, and selected hop beside
        those public slots.
        """
        if not hypotheses or not q_vec:
            return []
        active = list(hypotheses)
        if not all(isinstance(h, WalkHypothesis) for h in active):
            return []

        def _unit(vec: Sequence[float]) -> list[float]:
            norm = math.sqrt(sum(float(x) * float(x) for x in vec))
            if norm == 0:
                return []
            return [float(x) / norm for x in vec]

        q_unit = _unit(q_vec)
        if not q_unit:
            return []

        selected_rows: list[WalkRow] = []
        max_hops = max(0, min(int(hops), 2))
        for hop in range(1, max_hops + 1):
            if not active:
                break
            if hop == 2:
                allowed_destinations: list[int] = []
                for hypothesis in active:
                    if hypothesis.noun_id not in allowed_destinations:
                        allowed_destinations.append(hypothesis.noun_id)
                    if len(allowed_destinations) == 32:
                        break
                allowed = set(allowed_destinations)
                active = [h for h in active if h.noun_id in allowed]

            source_ids = list(dict.fromkeys(h.noun_id for h in active))
            async with self.pool.acquire() as conn:
                edge_rows = await conn.fetch(
                    """
                    SELECT e.id, e.src_noun, e.tgt_noun, e.e_src_vec, e.e_tgt_vec,
                           e.magnitude, e.provenance_turns, e.last_active_turn,
                           src.label AS src_label, tgt.label AS tgt_label
                      FROM semantic_edge e
                      JOIN noun src ON src.id = e.src_noun
                      JOIN noun tgt ON tgt.id = e.tgt_noun
                     WHERE e.src_noun = ANY($1::int[])
                       AND e.verb_type = 'mentions'
                    """,
                    source_ids,
                )
                destination_ids = list(
                    dict.fromkeys(int(row["tgt_noun"]) for row in edge_rows)
                )
                incident_rows = (
                    await conn.fetch(
                        """
                        SELECT id, src_noun, tgt_noun, e_tgt_vec, last_active_turn
                          FROM semantic_edge
                         WHERE verb_type = 'mentions'
                           AND (
                               src_noun = ANY($1::int[])
                               OR tgt_noun = ANY($1::int[])
                           )
                        """,
                        destination_ids,
                    )
                    if destination_ids
                    else []
                )

            outgoing: dict[int, list[Any]] = {}
            for edge in edge_rows:
                outgoing.setdefault(int(edge["src_noun"]), []).append(edge)

            candidates: list[tuple[float, WalkRow, WalkHypothesis]] = []
            for hypothesis in active:
                for edge in outgoing.get(hypothesis.noun_id, []):
                    src_vec = _parse_pgvector(edge["e_src_vec"])
                    tgt_vec = _parse_pgvector(edge["e_tgt_vec"])
                    src_cos = _cosine_similarity(q_unit, src_vec)
                    tgt_cos = _cosine_similarity(q_unit, tgt_vec)
                    if src_cos is None or tgt_cos is None:
                        continue
                    src_align = clamp_cos(src_cos)
                    tgt_align = clamp_cos(tgt_cos)

                    incident_vectors: list[list[float]] = []
                    target_id = int(edge["tgt_noun"])
                    cutoff = int(hypothesis.turn_id) - 32
                    for other in incident_rows:
                        if int(other["id"]) == int(edge["id"]):
                            continue
                        if target_id not in (
                            int(other["src_noun"]),
                            int(other["tgt_noun"]),
                        ):
                            continue
                        last_active = other["last_active_turn"]
                        if last_active is None or int(last_active) < cutoff:
                            continue
                        unit = _unit(_parse_pgvector(other["e_tgt_vec"]))
                        if unit:
                            incident_vectors.append(unit)

                    empty_incident = not incident_vectors
                    local_dir = query_dir = 0.0
                    if incident_vectors:
                        consensus = _unit(
                            [
                                sum(vec[i] for vec in incident_vectors)
                                / len(incident_vectors)
                                for i in range(len(incident_vectors[0]))
                            ]
                        )
                        local_dir = _cosine_similarity(tgt_vec, consensus) or 0.0
                        query_dir = _cosine_similarity(tgt_vec, q_unit) or 0.0
                    decay = consensus_decay(
                        empty_incident=empty_incident,
                        local_dir=local_dir,
                        query_dir=query_dir,
                        hop=hop,
                    )
                    c, _composite, score = beam_score(
                        sim=hypothesis.sim,
                        src_align=src_align,
                        tgt_align=tgt_align,
                        prov_boost=provenance_boost(
                            hypothesis.turn_id,
                            list(edge["provenance_turns"] or []),
                        ),
                        decay=decay,
                        magnitude=float(edge["magnitude"]),
                    )
                    if score < 0.05:
                        continue

                    audit = {
                        "session_id": hypothesis.session_id,
                        "turn_id": hypothesis.turn_id,
                        "chunk_id": hypothesis.chunk_id,
                        "hop": hop,
                        "provenance_turns": list(edge["provenance_turns"] or []),
                    }
                    row = WalkRow(
                        (
                            {
                                "id": str(edge["src_noun"]),
                                "name": str(edge["src_label"]),
                                "label": "Noun",
                            },
                            "mentions",
                            {
                                "id": str(edge["tgt_noun"]),
                                "name": str(edge["tgt_label"]),
                                "label": "Noun",
                            },
                            float(edge["magnitude"]),
                            c,
                            decay,
                            score,
                        ),
                        audit=audit,
                    )
                    child = WalkHypothesis(
                        noun_id=int(edge["tgt_noun"]),
                        chunk_id=hypothesis.chunk_id,
                        session_id=hypothesis.session_id,
                        turn_id=hypothesis.turn_id,
                        sim=hypothesis.sim,
                        chunk_vec=hypothesis.chunk_vec,
                    )
                    candidates.append((score, row, child))

            candidates.sort(key=lambda item: item[0], reverse=True)
            width = max(k * 4, 16) if hop == 1 else max(k * 2, 8)
            chosen = candidates[:width]
            selected_rows.extend(item[1] for item in chosen)
            active = [item[2] for item in chosen]

        return selected_rows

