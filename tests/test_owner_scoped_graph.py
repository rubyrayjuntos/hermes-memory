"""Graph expansion must never surface another memory owner's turns or labels."""

from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider
from hermes_memory.walk import WalkRow


def _row(src, dst, provenance):
    return WalkRow(
        ({"id": "1", "name": src, "label": "Noun"}, "mentions", {"id": "2", "name": dst, "label": "Noun"},
         4.0, 1.0, 1.0, 0.7),
        audit={"session_id": "s", "turn_id": 41, "chunk_id": "conv_41", "hop": 1, "provenance_turns": provenance},
    )


class Store:
    owners = {41: "alice", 42: "alice", 77: "bob", 88: "bob"}

    async def passports_for_conversations(self, conv_ids):
        return [{"noun_id": 11, "chunk_id": "conv_41", "session_id": "s", "turn_id": 41}]

    async def expand_graph(self, hypotheses, *, q_vec, hops, k):
        return [
            _row("Mara", "Lisbon", [41, 77]),     # mixed: keep, trim to alice's turn
            _row("Mara", "SecretBob", [88]),      # bob only: drop entirely
            _row("Mara", "Osaka", [42]),          # alice only: keep
        ]

    async def owned_turn_ids(self, ids, agent_identity):
        return {i for i in ids if self.owners.get(int(i)) == agent_identity}


def _provider(owner):
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.store = Store()
    p.config = HybridAgeConfig(embed_dim=2, vector_k=8)
    p._agent_identity = owner
    return p


SEEDS = [{"id": "41", "src": "conversation", "similarity": 0.8, "embedding": "[1,0]"}]


async def test_paths_from_another_owner_are_dropped_and_provenance_is_trimmed():
    paths = await _provider("alice")._expand_paths(SEEDS, q_vec=[1.0, 0.0])
    targets = {p["to_name"] for p in paths}
    assert targets == {"Lisbon", "Osaka"}
    assert "SecretBob" not in targets
    for p in paths:
        assert all(Store.owners[t] == "alice" for t in p["provenance_turns"])


async def test_owner_with_no_provenance_gets_no_graph_paths():
    paths = await _provider("carol")._expand_paths(SEEDS, q_vec=[1.0, 0.0])
    assert paths == []
