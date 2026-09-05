from hermes_memory.provider_helpers import format_triple, parse_agtype_vertex


def test_noun_dict_is_accepted_without_age_vertex_suffix() -> None:
    noun = {"id": "11", "name": "SourceNoun", "label": "Noun"}

    assert parse_agtype_vertex(noun) == noun
    assert (
        format_triple(
            noun,
            "mentions",
            {"id": "12", "name": "TargetNoun", "label": "Noun"},
        )
        == "[Noun SourceNoun] -mentions-> [Noun TargetNoun]"
    )


async def test_prefetch_expands_only_conversation_passports() -> None:
    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.provider import HybridAgeMemoryProvider
    from hermes_memory.walk import WalkRow

    class FakeStore:
        def __init__(self) -> None:
            self.hypotheses = None

        async def passports_for_conversations(self, conv_ids):
            assert conv_ids == [41]
            return [
                {
                    "noun_id": 11,
                    "chunk_id": "conv_41",
                    "session_id": "session-a",
                    "turn_id": 41,
                }
            ]

        async def expand_graph(self, hypotheses, *, q_vec, hops, k):
            self.hypotheses = hypotheses
            assert q_vec == [1.0, 0.0]
            assert hops == 2
            return [
                WalkRow(
                    (
                        {"id": "11", "name": "SourceNoun", "label": "Noun"},
                        "mentions",
                        {"id": "12", "name": "TargetNoun", "label": "Noun"},
                        4.0,
                        1.0,
                        1.0,
                        0.7,
                    ),
                    audit={
                        "session_id": "session-a",
                        "turn_id": 41,
                        "chunk_id": "conv_41",
                        "hop": 1,
                    },
                )
            ]

    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.store = FakeStore()
    provider.config = HybridAgeConfig(embed_dim=2, vector_k=8)
    seeds = [
        {
            "id": "41",
            "src": "conversation",
            "similarity": 0.8,
            "embedding": "[1,0]",
        },
        {
            "id": "doc:1",
            "src": "doc_chunk",
            "similarity": 0.9,
            "embedding": "[0,1]",
        },
    ]

    paths = await provider._expand_paths(seeds, q_vec=[1.0, 0.0])

    assert len(provider.store.hypotheses) == 1
    assert provider.store.hypotheses[0].noun_id == 11
    assert paths[0]["chunk_id"] == "conv_41"
    assert paths[0]["turn_id"] == 41
    assert paths[0]["session_id"] == "session-a"
    assert paths[0]["from_name"] == "SourceNoun"
    assert paths[0]["to_name"] == "TargetNoun"


async def test_prefetch_omits_debug_dsl_from_prompt() -> None:
    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.provider import HybridAgeMemoryProvider

    class Store:
        async def vector_search(self, _literal, _k):
            return [{
                "id": "doc:1",
                "src": "doc_chunk",
                "similarity": 0.9,
                "content": "Postgres catalog",
                "embedding": "[1,0]",
            }]

        async def passports_for_conversations(self, _conv_ids):
            return []

    class Embedder:
        async def embed_text(self, _text):
            return [1.0, 0.0]

    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.store = Store()
    provider.pool = None
    provider.embedder = Embedder()
    provider.config = HybridAgeConfig(embed_dim=2)
    provider._last_recall_count = 0
    provider._recent_topics = []
    provider._recent_turns = []

    block = await provider._aprefetch("postgres")

    assert "Postgres catalog" in block
    assert "<PAST_CONTEXT>" in block
    assert "persisted 7 turns · 5 nouns · 3 mentions" not in block
    assert "ABOUT" not in block
    assert "[SEED V" not in block


async def test_prefetch_injects_provenance_turn_body_not_cypher() -> None:
    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.provider import HybridAgeMemoryProvider
    from hermes_memory.walk import WalkRow

    class Store:
        async def vector_search(self, _literal, _k):
            return [{
                "id": "41",
                "src": "conversation",
                "similarity": 0.8,
                "content": "seed turn about hybrid memory",
                "embedding": "[1,0]",
                "session_id": "s1",
                "ts": "2026-09-02T16:35:00+00:00",
            }]

        async def passports_for_conversations(self, conv_ids):
            assert conv_ids == [41]
            return [{
                "noun_id": 11,
                "chunk_id": "conv_41",
                "session_id": "s1",
                "turn_id": 41,
            }]

        async def expand_graph(self, _hypotheses, *, q_vec, hops, k):
            del q_vec, hops, k
            return [
                WalkRow(
                    (
                        {"id": "11", "name": "RECAP", "label": "Noun"},
                        "mentions",
                        {"id": "12", "name": "hermes-memory", "label": "Noun"},
                        5.8,
                        0.18,
                        0.97,
                        0.37,
                    ),
                    audit={
                        "session_id": "s1",
                        "turn_id": 41,
                        "chunk_id": "conv_41",
                        "hop": 1,
                        "provenance_turns": [41, 99],
                    },
                )
            ]

        async def conversations_by_ids(self, ids):
            assert 99 in [int(i) for i in ids]
            return [{
                "id": "99",
                "session_id": "s2",
                "content": "You wrote RECAP-2026-09-02-hermes-memory.md as a handoff.",
                "ts": "2026-09-02T17:00:00+00:00",
            }]

    class Embedder:
        async def embed_text(self, _text):
            return [1.0, 0.0]

    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.store = Store()
    provider.pool = None
    provider.embedder = Embedder()
    provider.config = HybridAgeConfig(embed_dim=2, min_similarity=0.5)
    provider._last_recall_count = 0
    provider._recent_topics = []
    provider._recent_turns = []

    block = await provider._aprefetch("hybrid age memory")

    assert "seed turn about hybrid memory" in block
    assert "You wrote RECAP-2026-09-02-hermes-memory.md as a handoff." in block
    assert "<PAST_CONTEXT>" in block
    assert "-mentions->" not in block
    assert "w=5.80" not in block
    assert "[SEED V" not in block

