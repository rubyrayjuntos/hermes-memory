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
