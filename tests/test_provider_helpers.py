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


async def test_prefetch_graph_line_reports_sql_manifold_without_about() -> None:
    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.provider import HybridAgeMemoryProvider

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Conn:
        def transaction(self):
            return Context()

        async def execute(self, *_args):
            return None

        async def fetch(self, query):
            if "MATCH (n)" in query:
                return [{"c": 12}]
            if "MATCH ()-[r]->()" in query:
                return [{"c": 8}]
            if "ABOUT" in query:
                return [{"c": 3}]
            return []

        async def fetchval(self, query):
            if "FROM conversations" in query:
                return 7
            if "FROM noun" in query:
                return 5
            if "FROM semantic_edge" in query:
                return 3
            return 0

    class Pool:
        def acquire(self):
            class Acquire(Context):
                async def __aenter__(self):
                    return Conn()

            return Acquire()

    class Store:
        graph_name = "hermes_knowledge"

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

        async def load_age(self, _conn):
            return None

    class Embedder:
        async def embed_text(self, _text):
            return [1.0, 0.0]

    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.store = Store()
    provider.pool = Pool()
    provider.embedder = Embedder()
    provider.config = HybridAgeConfig(embed_dim=2)
    provider._last_recall_count = 0
    provider._recent_topics = []
    provider._recent_turns = []

    block = await provider._aprefetch("postgres")

    assert "persisted 7 turns · 5 nouns · 3 mentions" in block
    assert "ABOUT" not in block
