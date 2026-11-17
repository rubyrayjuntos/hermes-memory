"""Shadow replay: real vector prefetch vs retrieve_hybrid, no swallowed errors."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_memory.config import HybridAgeConfig
from hermes_memory.embed import vec_to_literal
from hermes_memory.provider import HybridAgeMemoryProvider

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "shadow_replay.py"
_SPEC = importlib.util.spec_from_file_location("shadow_replay_script", _SCRIPT)
assert _SPEC and _SPEC.loader
shadow_replay = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(shadow_replay)

compute_hybrid_path = shadow_replay.compute_hybrid_path
compute_vector_path = shadow_replay.compute_vector_path
jaccard = shadow_replay.jaccard
replay_turns = shadow_replay.replay_turns


class FakeEmbedder:
    async def embed_text(self, text: str, *, as_query: bool = False):
        digest = hashlib.sha256((text or "").encode("utf-8")).digest()
        vec = [0.0] * 768
        vec[0] = ((digest[0] % 97) / 97.0) or 0.01
        vec[1] = ((digest[1] % 97) / 97.0) or 0.02
        vec[2] = ((digest[2] % 97) / 97.0) or 0.03
        vec[3] = 0.04
        return vec


class SeededStore:
    """Returns a conversation hit and a grounded claim for the seeded query."""

    def __init__(self, *, boom: bool = False):
        self.boom = boom
        self.pool = SimpleNamespace()

    async def vector_search(self, _vec_literal, _k, *, agent_identity=None):
        if self.boom:
            raise RuntimeError("ann failed")
        assert agent_identity == "owner-a"
        return [
            {
                "id": "914",
                "src": "conversation",
                "content": "Acme uses Postgres.",
                "similarity": 0.99,
                "session_id": "session-a",
                "ts": None,
            }
        ]

    async def claim_vector_search(self, owner, _vec_literal, _limit):
        assert owner == "owner-a"
        return [
            {
                "claim_id": 7,
                "subject_id": 11,
                "object_id": 12,
                "fact_text": "Acme uses Postgres.",
                "fact_vec": "[1,0]",
            }
        ]

    async def link_entities(self, owner, _phrases, _vec_literal):
        assert owner == "owner-a"
        return [{"entity_id": 11}]

    async def turn_entities(self, owner, turn_ids):
        assert owner == "owner-a"
        return [{"span_id": 914, "entity_id": 11}]

    async def claims_for_turns(self, owner, turn_ids):
        assert owner == "owner-a"
        return ["7"]

    async def claim_edges(self, owner, entity_ids, _vec_literal, per_node):
        assert owner == "owner-a"
        return []

    async def claims_by_ids(self, owner, claim_ids):
        assert owner == "owner-a"
        return [
            {
                "claim_id": 7,
                "status": "grounded",
                "fact_text": "Acme uses Postgres.",
                "fact_vec": "[1,0]",
                "span_id": 914,
                "event_time": None,
                "role": "user",
                "content": "Acme uses Postgres.",
                "session_id": "session-a",
            }
        ]


def _provider(store) -> HybridAgeMemoryProvider:
    cfg = HybridAgeConfig(min_similarity=0.0, retrieval_mode="vector", prefetch_timeout_s=15.0)
    provider = HybridAgeMemoryProvider(config=cfg)
    provider.store = store
    provider.embedder = FakeEmbedder()
    provider.pool = getattr(store, "pool", None)
    return provider


@pytest.mark.asyncio
async def test_seeded_paths_are_not_both_empty():
    provider = _provider(SeededStore())
    query = "What database does Acme use?"
    vector = await compute_vector_path(provider, query, "owner-a")
    hybrid = await compute_hybrid_path(provider, query, "owner-a")
    if not vector["ids"] and not hybrid["ids"]:
        pytest.fail("both paths returned empty for a seeded case")
    assert vector["ids"], vector
    assert hybrid["ids"], hybrid
    assert vector["tokens"] > 0
    assert hybrid["tokens"] > 0


@pytest.mark.asyncio
async def test_vector_path_does_not_hide_exceptions():
    provider = _provider(SeededStore(boom=True))
    with pytest.raises(RuntimeError, match="ann failed"):
        await compute_vector_path(provider, "What database does Acme use?", "owner-a")


@pytest.mark.asyncio
async def test_hybrid_path_does_not_hide_exceptions():
    store = SeededStore()

    async def boom_claims(*_a, **_k):
        raise RuntimeError("claim ann failed")

    store.claim_vector_search = boom_claims  # type: ignore[method-assign]
    provider = _provider(store)
    with pytest.raises(RuntimeError, match="claim ann failed"):
        await compute_hybrid_path(provider, "What database does Acme use?", "owner-a")


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return None


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _Conn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_replay_counts_errors_instead_of_swallowing():
    store = SeededStore(boom=True)
    provider = _provider(store)
    provider.pool = _Pool(_Conn())
    turns = [{"id": "1", "content": "What database does Acme use?", "owner": "owner-a"}]
    report = await replay_turns(provider, turns, secret=b"\x00" * 32, dry_run=True)
    assert report["vector_errors"] == 1
    assert report["turns_compared"] == 0
    assert report["vector_error_rate"] == 1.0


def test_jaccard_empty_both_is_one():
    assert jaccard([], []) == 1.0
    assert jaccard(["1"], ["1", "2"]) == 0.5


@pytest.mark.integration
@pytest.mark.store
@pytest.mark.asyncio
async def test_shadow_replay_integration_fixtures(store, hermes_test_dsn):
    embedder = FakeEmbedder()
    content = "Acme uses Postgres for the replay integration fixture."
    vec = await embedder.embed_text(content)
    await store.insert_turn(
        "replay-int",
        "replay-owner",
        "user",
        content,
        vec_to_literal(vec),
        metadata={"kind": "interactive"},
    )
    cfg = HybridAgeConfig(
        dsn=hermes_test_dsn,
        min_similarity=0.0,
        retrieval_mode="vector",
        prefetch_timeout_s=15.0,
    )
    provider = HybridAgeMemoryProvider(config=cfg)
    provider.store = store
    provider.embedder = embedder
    provider.pool = store.pool
    vector = await compute_vector_path(provider, "What does Acme use?", "replay-owner")
    hybrid = await compute_hybrid_path(provider, "What does Acme use?", "replay-owner")
    if not vector["ids"] and not hybrid["ids"]:
        pytest.fail("both paths returned empty against migrated fixtures")
    assert vector["ids"], vector
