"""Integration: stronger mentions rank first in expand_graph.

Legacy AGE ABOUT recency (edge/vertex created_at, decay 0.5) is gone.
expand_graph walks SQL mentions and scores via beam_score × magnitude
(src/hermes_memory/store_expand.py). Same alignment + provenance +
consensus_decay: higher magnitude (the live stand-in for a newer,
reinforced edge) ranks first. last_active_ts is not read for the
scored edge.
"""
from __future__ import annotations

import pytest

from hermes_memory.embed import vec_to_literal
from hermes_memory.walk import WalkHypothesis

pytestmark = [pytest.mark.store, pytest.mark.integration]


def _axis(index: int) -> list[float]:
    vec = [0.0] * 768
    vec[index] = 1.0
    return vec


@pytest.mark.asyncio
async def test_expand_graph_stronger_mentions_ranks_first(
    store, clean_hermes_test_db,
) -> None:
    src = await store.upsert_noun("RecencySrc")
    newer = await store.upsert_noun("RecencyDstNew")
    older = await store.upsert_noun("RecencyDstOld")
    turn_id = await store.insert_turn(
        "session-recency",
        "default",
        "user",
        "RecencySrc mentions RecencyDstNew and RecencyDstOld",
        vec_to_literal(_axis(0)),
        {"kind": "interactive"},
    )
    assert turn_id is not None
    await store.upsert_mentions_chain(
        [(src, newer), (src, older)],
        turn_id=turn_id,
        turn_vec=_axis(0),
        src_vecs={src: _axis(0)},
        confs={(src, newer): 6.0, (src, older): 2.0},
    )

    hypothesis = WalkHypothesis(
        noun_id=src,
        chunk_id=f"conv_{turn_id}",
        session_id="session-recency",
        turn_id=turn_id,
        sim=0.8,
        chunk_vec=_axis(0),
    )
    rows = await store.expand_graph([hypothesis], q_vec=_axis(0), hops=1, k=8)
    mentions = [r for r in rows if r[1] == "mentions"]
    names = [r[2]["name"] for r in mentions]
    assert "RecencyDstNew" in names and "RecencyDstOld" in names, names
    assert names.index("RecencyDstNew") < names.index("RecencyDstOld"), names
    assert mentions[0][3] == 6.0
    assert mentions[1][3] == 2.0
