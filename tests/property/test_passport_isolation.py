"""P0: shared nouns never let conversation passports bleed across sessions."""
from __future__ import annotations

import hashlib

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hermes_memory.graph_api import pack_search
from hermes_memory.provider import HybridAgeMemoryProvider
from hermes_memory.walk import WalkHypothesis, parse_embedding

pytestmark = [pytest.mark.store, pytest.mark.integration]

LABELS = ("RateLimiter", "VectorRouter", "TokenBudget", "QueryPlanner", "MemoryGraph")


class FakeEmbedder:
    """Deterministic 768-d embedder; manifold property tests never call Ollama."""

    async def embed_text(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [0.0] * 768
        vector[0] = (digest[0] % 97) / 97.0 or 0.01
        vector[1] = (digest[1] % 97) / 97.0 or 0.02
        vector[2] = (digest[2] % 97) / 97.0 or 0.03
        vector[3] = 0.04
        return vector


def _make_provider(store) -> HybridAgeMemoryProvider:
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.config = None
    provider.pool = store.pool
    provider.store = store
    provider.embedder = FakeEmbedder()
    provider._agent_identity = "default"
    provider._session_id = "default"
    provider._last_turn_id = {}
    provider._concept_emb = {}
    provider._concept_names = None
    provider._write_queue = None
    provider._loop = None
    provider._primary_context = True
    provider._recent_topics = []
    provider._recent_turns = []
    provider._max_topics = 8
    provider._max_turns = 6
    return provider


def _turn_content(labels: list[str], index: int) -> str:
    return " connects to ".join(f"`{label}`" for label in labels) + f" iteration {index}"


async def _reset_property_example(db_pool) -> None:
    """Hypothesis reuses function fixtures, so isolate every generated example."""
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        await conn.fetch(
            """
            SELECT * FROM cypher('hermes_knowledge', $$
                MATCH (t:Turn) DETACH DELETE t
                RETURN count(t)
            $$) AS (deleted agtype)
            """
        )
        await conn.execute(
            """
            TRUNCATE conversations, memory_entries, memory_chunk_nodes,
                     doc_chunks, librarian_runs, semantic_edge, noun
                     RESTART IDENTITY CASCADE
            """
        )


async def _write_session(
    provider: HybridAgeMemoryProvider,
    session_id: str,
    labels: list[str],
    turn_count: int,
) -> list[int]:
    turn_ids: list[int] = []
    previous_turn_id = None
    for index in range(turn_count):
        await provider._awrite_item(
            {
                "type": "turn",
                "session_id": session_id,
                "role": "user" if index % 2 == 0 else "assistant",
                "content": _turn_content(labels, index),
                "previous_conversation_id": previous_turn_id,
            }
        )
        previous_turn_id = provider._last_turn_id[session_id]
        turn_ids.append(previous_turn_id)
    return turn_ids


async def _next_pairs(db_pool, session_id: str) -> set[tuple[int, int]]:
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        rows = await conn.fetch(
            """
            SELECT * FROM cypher('hermes_knowledge', $$
                MATCH (a:Turn)-[:NEXT]->(b:Turn)
                RETURN a.turn_id, b.turn_id, a.session_id, b.session_id
            $$) AS (src agtype, tgt agtype, src_session agtype, tgt_session agtype)
            """
        )
    return {
        (
            int(str(row["src"]).strip().strip('"')),
            int(str(row["tgt"]).strip().strip('"')),
        )
        for row in rows
        if session_id
        in {
            str(row["src_session"]).strip().strip('"'),
            str(row["tgt_session"]).strip().strip('"'),
        }
    }


@given(
    session_a=st.integers(min_value=0, max_value=999).map(lambda n: f"sess-a-{n}"),
    session_b=st.integers(min_value=0, max_value=999).map(lambda n: f"sess-b-{n}"),
    labels=st.lists(st.sampled_from(LABELS), min_size=1, max_size=5, unique=True),
    turns_a=st.integers(min_value=1, max_value=3),
    turns_b=st.integers(min_value=1, max_value=3),
)
@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@pytest.mark.asyncio
async def test_two_sessions_share_nouns_without_passport_bleed(
    session_a: str,
    session_b: str,
    labels: list[str],
    turns_a: int,
    turns_b: int,
    db_pool,
    store,
    clean_hermes_test_db,
    hermes_test_dsn: str,
) -> None:
    assert "hermes_test" in hermes_test_dsn
    assert "hermes_memory" not in hermes_test_dsn
    assert session_a != session_b
    await _reset_property_example(db_pool)

    provider = _make_provider(store)
    a_turn_ids = await _write_session(provider, session_a, labels, turns_a)
    b_turn_ids = await _write_session(provider, session_b, list(reversed(labels)), turns_b)
    a_turn_set = set(a_turn_ids)
    b_turn_set = set(b_turn_ids)
    assert a_turn_set.isdisjoint(b_turn_set)

    async with db_pool.acquire() as conn:
        noun_rows = await conn.fetch(
            "SELECT id, label FROM noun WHERE label = ANY($1::text[]) ORDER BY label",
            labels,
        )
        passport_rows = await conn.fetch(
            """
            SELECT mcn.noun_id, mcn.session_id, mcn.turn_id, mcn.chunk_id, n.label
              FROM memory_chunk_nodes mcn
              JOIN noun n ON n.id = mcn.noun_id
             WHERE n.label = ANY($1::text[])
               AND mcn.session_id = ANY($2::text[])
             ORDER BY mcn.noun_id, mcn.turn_id
            """,
            labels,
            [session_a, session_b],
        )
        conversation_rows = await conn.fetch(
            """
            SELECT id, session_id, content, embedding
              FROM conversations
             WHERE id = ANY($1::bigint[])
             ORDER BY id
            """,
            a_turn_ids + b_turn_ids,
        )
        edge_rows = await conn.fetch(
            """
            SELECT src_noun, tgt_noun, provenance_turns
              FROM semantic_edge
             WHERE verb_type = 'mentions'
            """
        )

    noun_ids = {str(row["label"]): int(row["id"]) for row in noun_rows}
    assert set(noun_ids) == set(labels)
    assert len(noun_rows) == len(labels), "each shared canonical label has one global noun"

    expected_passports = {
        (noun_ids[label], session_id, turn_id, f"conv_{turn_id}")
        for label in labels
        for session_id, turn_ids in (
            (session_a, a_turn_ids),
            (session_b, b_turn_ids),
        )
        for turn_id in turn_ids
    }
    actual_passports = {
        (
            int(row["noun_id"]),
            str(row["session_id"]),
            int(row["turn_id"]),
            str(row["chunk_id"]),
        )
        for row in passport_rows
    }
    assert actual_passports == expected_passports

    conversations = {int(row["id"]): dict(row) for row in conversation_rows}
    a_passports = [
        row
        for row in passport_rows
        if str(row["session_id"]) == session_a
    ]
    hypotheses = [
        WalkHypothesis(
            noun_id=int(passport["noun_id"]),
            chunk_id=str(passport["chunk_id"]),
            session_id=session_a,
            turn_id=int(passport["turn_id"]),
            sim=0.9,
            chunk_vec=parse_embedding(conversations[int(passport["turn_id"])]["embedding"]),
        )
        for passport in a_passports
    ]
    q_vec = parse_embedding(conversations[a_turn_ids[0]]["embedding"])
    rows = await store.expand_graph(hypotheses, q_vec=q_vec, hops=2, k=64)

    provenance_by_edge = {
        (int(edge["src_noun"]), int(edge["tgt_noun"])): {
            int(turn_id) for turn_id in (edge["provenance_turns"] or [])
        }
        for edge in edge_rows
    }
    observed_boosts: set[float] = set()
    for row in rows:
        assert row.audit["session_id"] == session_a
        assert int(row.audit["turn_id"]) in a_turn_set
        assert int(row.audit["turn_id"]) not in b_turn_set
        src_noun = int(row[0]["id"])
        tgt_noun = int(row[2]["id"])
        provenance_turns = provenance_by_edge[(src_noun, tgt_noun)]
        prov_boost = 1.0 if int(row.audit["turn_id"]) in provenance_turns else 0.0
        observed_boosts.add(prov_boost)
        expected_score = (
            0.4 * 0.9 + 0.4 * float(row[4]) * prov_boost + 0.2 * float(row[5])
        ) * (float(row[3]) / 8.0)
        assert float(row[6]) == pytest.approx(expected_score, abs=1e-6)

    if len(labels) == 1:
        assert rows == []
    else:
        assert observed_boosts == {0.0, 1.0}
        assert any(row.audit["hop"] == 2 for row in rows)

    a_seeds = [
        {
            "id": turn_id,
            "src": "conversation",
            "content": str(conversations[turn_id]["content"]),
            "similarity": 0.9,
        }
        for turn_id in a_turn_ids
    ]
    packed = pack_search(
        "shared noun",
        64,
        2,
        a_seeds,
        [str(noun_ids[label]) for label in labels],
        rows,
    )
    assert {seed["chunk_id"] for seed in packed["seeds"]} == {
        f"conv_{turn_id}" for turn_id in a_turn_ids
    }
    assert all(
        seed["excerpt"] == str(conversations[int(seed["chunk_id"][5:])]["content"])[:160]
        for seed in packed["seeds"]
    )
    assert not {
        seed["chunk_id"] for seed in packed["seeds"]
    }.intersection({f"conv_{turn_id}" for turn_id in b_turn_ids})

    a_next_pairs = await _next_pairs(db_pool, session_a)
    assert a_next_pairs == set(zip(a_turn_ids, a_turn_ids[1:]))
    assert all(src not in b_turn_set and tgt not in b_turn_set for src, tgt in a_next_pairs)
