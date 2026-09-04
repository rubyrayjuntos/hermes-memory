"""Store noun / passport / mentions writes against hermes_test (spec §10.2 store slice)."""
from __future__ import annotations

import pytest

from hermes_memory.embed import vec_to_literal

pytestmark = [pytest.mark.store, pytest.mark.integration]


def _assert_hermes_test_dsn(dsn: str) -> None:
    assert "hermes_test" in dsn
    assert "hermes_memory" not in dsn


def _unit_vec(seed: int) -> list[float]:
    v = [0.0] * 768
    v[0] = 0.25 + (seed % 50) / 100.0
    v[1] = 0.10 + (seed % 17) / 100.0
    v[2] = 0.05
    v[3] = 0.02
    return v


def _parse_vec(raw) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x.strip()]


@pytest.mark.asyncio
async def test_upsert_noun_passports_and_mentions_chain(
    db_pool, store, clean_hermes_test_db, hermes_test_dsn,
):
    _assert_hermes_test_dsn(hermes_test_dsn)

    n1 = await store.upsert_noun("RateLimiter", "tool")
    n2 = await store.upsert_noun("nomic-embed-text", "technology")
    assert n1 != n2
    again = await store.upsert_noun("RateLimiter", "tool")
    assert again == n1

    turn_id = await store.insert_turn(
        "sess-manifold-writes", "default", "user",
        "RateLimiter talks to nomic-embed-text",
        vec_to_literal(_unit_vec(1)),
        {"kind": "interactive"},
    )
    assert turn_id is not None
    chunk_id = f"conv_{turn_id}"

    rows = [
        {
            "chunk_id": chunk_id,
            "source": "conversation",
            "noun_id": n1,
            "vertex_id": None,
            "session_id": "sess-manifold-writes",
            "turn_id": turn_id,
            "conf": 0.75,
            "graph_name": "hermes_knowledge",
        },
        {
            "chunk_id": chunk_id,
            "source": "conversation",
            "noun_id": n2,
            "vertex_id": None,
            "session_id": "sess-manifold-writes",
            "turn_id": turn_id,
            "conf": 0.75,
            "graph_name": "hermes_knowledge",
        },
    ]
    await store.write_passports(rows)

    async with db_pool.acquire() as conn:
        got = await conn.fetch(
            "SELECT noun_id, conf, chunk_id, source FROM memory_chunk_nodes "
            "WHERE chunk_id=$1 AND source='conversation' AND noun_id IS NOT NULL "
            "ORDER BY noun_id",
            chunk_id,
        )
    assert len(got) == 2
    assert {r["noun_id"] for r in got} == {n1, n2}

    rows[0]["conf"] = 0.90
    rows[1]["conf"] = 0.90
    await store.write_passports(rows)
    async with db_pool.acquire() as conn:
        confs = await conn.fetch(
            "SELECT conf FROM memory_chunk_nodes WHERE chunk_id=$1 AND noun_id IS NOT NULL",
            chunk_id,
        )
    assert all(abs(float(r["conf"]) - 0.90) < 1e-9 for r in confs)

    listed = await store.passports_for_conversations([turn_id])
    assert len(listed) == 2
    assert all(p["chunk_id"] == chunk_id for p in listed)
    assert all(p["source"] == "conversation" for p in listed)
    assert all(p["noun_id"] is not None for p in listed)

    src_vec = _unit_vec(7)
    turn_vec = _unit_vec(3)
    pair = (n1, n2)
    await store.upsert_mentions_chain(
        [pair],
        turn_id=turn_id,
        turn_vec=turn_vec,
        src_vecs={n1: src_vec},
        confs={pair: 0.90},
    )
    async with db_pool.acquire() as conn:
        edge = await conn.fetchrow(
            "SELECT magnitude, polarity, verb_type, e_src_vec, e_tgt_vec, provenance_turns "
            "FROM semantic_edge WHERE src_noun=$1 AND tgt_noun=$2 AND verb_type='mentions'",
            n1, n2,
        )
    assert edge is not None
    assert edge["verb_type"] == "mentions"
    assert int(edge["polarity"]) == 1
    assert abs(float(edge["magnitude"]) - 0.90) < 1e-9
    first_src = _parse_vec(edge["e_src_vec"])[:4]
    first_tgt = _parse_vec(edge["e_tgt_vec"])[:4]
    assert first_src == pytest.approx(src_vec[:4], abs=1e-5)

    stronger = _unit_vec(11)
    for _ in range(12):
        await store.upsert_mentions_chain(
            [pair],
            turn_id=turn_id,
            turn_vec=stronger,
            src_vecs={n1: src_vec},
            confs={pair: 0.90},
        )
    async with db_pool.acquire() as conn:
        edge2 = await conn.fetchrow(
            "SELECT magnitude, e_src_vec, e_tgt_vec, provenance_turns "
            "FROM semantic_edge WHERE src_noun=$1 AND tgt_noun=$2 AND verb_type='mentions'",
            n1, n2,
        )
    assert float(edge2["magnitude"]) == 8.0
    assert _parse_vec(edge2["e_src_vec"])[:4] == pytest.approx(first_src, abs=1e-5)
    assert _parse_vec(edge2["e_tgt_vec"])[:4] != pytest.approx(first_tgt, abs=1e-4)
    prov = list(edge2["provenance_turns"] or [])
    assert turn_id in prov
    assert len(prov) == len(set(prov))
    assert len(prov) <= 32

    await store.upsert_mentions_chain(
        [(n2, n1)],
        turn_id=turn_id,
        turn_vec=turn_vec,
        src_vecs={},
        confs={(n2, n1): 0.5},
    )
    async with db_pool.acquire() as conn:
        reverse = await conn.fetchval(
            "SELECT count(*) FROM semantic_edge WHERE src_noun=$1 AND tgt_noun=$2",
            n2, n1,
        )
    assert int(reverse) == 0
