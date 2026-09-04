"""Integration coverage for the bounded semantic_edge mentions beam."""
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
async def test_expand_mentions_returns_scored_seven_tuple(
    db_pool, store, clean_hermes_test_db,
) -> None:
    src = await store.upsert_noun("SourceNoun", "tool")
    tgt = await store.upsert_noun("TargetNoun", "technology")
    turn_id = await store.insert_turn(
        "session-a",
        "default",
        "user",
        "SourceNoun mentions TargetNoun",
        vec_to_literal(_axis(0)),
        {"kind": "interactive"},
    )
    assert turn_id is not None
    await store.write_passports(
        [
            {
                "chunk_id": f"conv_{turn_id}",
                "source": "conversation",
                "noun_id": src,
                "session_id": "session-a",
                "turn_id": turn_id,
                "conf": 1.0,
            }
        ]
    )
    await store.upsert_mentions_chain(
        [(src, tgt)],
        turn_id=turn_id,
        turn_vec=_axis(0),
        src_vecs={src: _axis(0)},
        confs={(src, tgt): 4.0},
    )

    hypothesis = WalkHypothesis(
        noun_id=src,
        chunk_id=f"conv_{turn_id}",
        session_id="session-a",
        turn_id=turn_id,
        sim=0.8,
        chunk_vec=_axis(0),
    )
    rows = await store.expand_graph([hypothesis], q_vec=_axis(0), hops=1, k=8)

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, tuple)
    assert len(row) == 7
    n, rel, m, w, c, decay, score = row
    assert n == {"id": str(src), "name": "SourceNoun", "label": "Noun"}
    assert m == {"id": str(tgt), "name": "TargetNoun", "label": "Noun"}
    assert rel == "mentions"
    assert w == 4.0
    assert c == pytest.approx(1.0)
    expected = (0.4 * 0.8 + 0.4 * c * 1.0 + 0.2 * decay) * (w / 8.0)
    assert score == pytest.approx(expected, abs=1e-6)
    assert row.audit == {
        "session_id": "session-a",
        "turn_id": turn_id,
        "chunk_id": f"conv_{turn_id}",
        "hop": 1,
    }


@pytest.mark.asyncio
async def test_second_hop_keeps_parent_passport_session(
    store, clean_hermes_test_db,
) -> None:
    first = await store.upsert_noun("FirstNoun")
    second = await store.upsert_noun("SecondNoun")
    third = await store.upsert_noun("ThirdNoun")
    turn_id = await store.insert_turn(
        "parent-session",
        "default",
        "user",
        "FirstNoun then SecondNoun then ThirdNoun",
        vec_to_literal(_axis(0)),
        {"kind": "interactive"},
    )
    assert turn_id is not None
    await store.upsert_mentions_chain(
        [(first, second), (second, third)],
        turn_id=turn_id,
        turn_vec=_axis(0),
        src_vecs={first: _axis(0), second: _axis(0)},
        confs={(first, second): 4.0, (second, third): 4.0},
    )
    hypothesis = WalkHypothesis(
        noun_id=first,
        chunk_id=f"conv_{turn_id}",
        session_id="parent-session",
        turn_id=turn_id,
        sim=0.9,
        chunk_vec=_axis(0),
    )

    rows = await store.expand_graph([hypothesis], q_vec=_axis(0), hops=2, k=8)

    hop_two = [row for row in rows if row.audit["hop"] == 2]
    assert hop_two
    assert all(row.audit["session_id"] == "parent-session" for row in hop_two)
    assert all(row.audit["turn_id"] == turn_id for row in hop_two)


@pytest.mark.asyncio
async def test_empty_or_legacy_integer_hypotheses_do_not_walk_age(store) -> None:
    assert await store.expand_graph([], q_vec=_axis(0)) == []
    assert await store.expand_graph([123], q_vec=_axis(0)) == []
