"""Pane /search: owner scoping and the same query text as prefetch."""

from types import SimpleNamespace

from hermes_memory.graph_runtime import Runtime
from hermes_memory.tokens import QUERY_TOKEN_CAP, truncate_tokens
from hermes_memory.walk import WalkRow


class Store:
    hnsw_ef_search = 100

    def __init__(self):
        self.vector_calls = []

    async def vector_search(self, literal, k, *, agent_identity=None):
        self.vector_calls.append(agent_identity)
        return []

    async def passports_for_conversations(self, ids):
        return []

    async def owned_turn_ids(self, ids, owner):
        return {i for i in ids if i in {41, 42}}


class Embedder:
    def __init__(self):
        self.texts = []

    async def embed_text(self, text):
        self.texts.append(text)
        return [1.0, 0.0]


def _runtime():
    rt = Runtime.__new__(Runtime)
    rt.store, rt.embedder = Store(), Embedder()
    rt.cfg = SimpleNamespace(min_similarity=0.55, embed_model="nomic-embed-text", embed_dim=2)
    return rt


async def test_search_passes_owner_and_embeds_the_prefetch_query_text():
    rt = _runtime()
    long_query = "memory " * 5000
    await rt._asearch(long_query, 8, 2, owner="alice")
    assert rt.store.vector_calls == ["alice"]
    assert rt.embedder.texts == [truncate_tokens(long_query, QUERY_TOKEN_CAP)]


async def test_search_without_owner_is_the_all_owners_view():
    rt = _runtime()
    await rt._asearch("memory", 8, 2)
    assert rt.store.vector_calls == [None]


async def test_owner_rows_drop_foreign_provenance():
    rt = _runtime()
    rows = [WalkRow(("a", "mentions", "b", 1, 1, 1, 0.5), audit={"hop": 1, "provenance_turns": [41, 77]}),
            WalkRow(("a", "mentions", "c", 1, 1, 1, 0.5), audit={"hop": 1, "provenance_turns": [88]})]
    kept = await rt._owner_rows(rows, "alice")
    assert [r.audit["provenance_turns"] for r in kept] == [[41]]
