"""Shadow retrieval log: query_key, record_shadow, and the prefetch hook."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_memory import shadow
from hermes_memory.shadow import query_key, record_shadow, schedule_shadow

# -- query_key ---------------------------------------------------------------


def test_query_key_is_16_bytes_and_stable():
    k = query_key(b"secret", "what does Acme use?")
    assert isinstance(k, bytes) and len(k) == 16
    assert k == query_key(b"secret", "what does Acme use?")


def test_query_key_strips_the_query():
    assert query_key(b"s", "  hello \n") == query_key(b"s", "hello")


def test_query_key_differs_by_secret_and_text():
    assert query_key(b"a", "q") != query_key(b"b", "q")
    assert query_key(b"a", "q1") != query_key(b"a", "q2")


# -- record_shadow -----------------------------------------------------------


class FakeConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "INSERT 0 1"


async def test_record_shadow_binds_parameters_and_stores_no_text():
    conn = FakeConn()
    key = query_key(b"s", "secret question text")
    await record_shadow(conn, "owner1", key, ["a", 2], ["b"], {"vector": 1.5})
    (sql, args), = conn.calls
    assert "retrieval_shadow" in sql and "$1" in sql and "$5" in sql
    assert "owner1" not in sql and "secret question" not in sql
    assert args[0] == "owner1" and args[1] == key
    assert json.loads(args[2]) == ["a", 2]
    assert json.loads(args[3]) == ["b"]
    assert json.loads(args[4]) == {"vector": 1.5}
    assert not any("secret question" in str(a) for a in args)


@pytest.mark.parametrize("bad", [b"", b"x" * 15, b"x" * 17])
async def test_record_shadow_rejects_wrong_key_length(bad):
    conn = FakeConn()
    with pytest.raises(ValueError):
        await record_shadow(conn, "o", bad, [], [], {})
    assert conn.calls == []


# -- schedule_shadow ---------------------------------------------------------


async def test_schedule_returns_before_hook_runs_and_swallows_errors():
    ran = []

    async def boom(*a):
        ran.append(a)
        raise RuntimeError("hook exploded")

    schedule_shadow(boom, "q", ["x"], {})
    assert ran == []  # not awaited inline
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ran == [("q", ["x"], {})]
    assert not shadow._TASKS  # finished tasks are released


async def test_schedule_swallows_sync_raise_and_non_awaitable():
    def sync_boom(*a):
        raise RuntimeError("sync")

    schedule_shadow(sync_boom, 1)
    schedule_shadow(lambda *a: None, 1)  # returns non-awaitable
    schedule_shadow(None, 1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not shadow._TASKS


# -- prefetch hook -----------------------------------------------------------


class FakeEmbedder:
    async def embed_text(self, text, *, as_query: bool = False):
        return [0.1, 0.2, 0.3]


class FakeStore:
    hnsw_ef_search = 100

    async def vector_search(self, lit, k, agent_identity=None):
        return [
            {"id": "conv_7", "src": "conversation", "similarity": 0.9,
             "content": "Acme uses Postgres.", "session_id": "s"},
            {"id": "conv_8", "src": "conversation", "similarity": 0.8,
             "content": "Beta uses Redis.", "session_id": "s"},
        ]


def make_provider(hook=None):
    from hermes_memory.provider import HybridAgeMemoryProvider

    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.config = SimpleNamespace(
        prefetch_timeout_s=5.0, vector_k=5, min_similarity=0.1, max_tokens=2000,
    )
    p.embedder = FakeEmbedder()
    p.store = FakeStore()
    p._agent_identity = "agent"
    p._recent_topics = []
    p._recent_turns = []
    if hook is not None:
        p._shadow_hook = hook
    return p


async def test_prefetch_string_is_unchanged_with_a_raising_hook():
    baseline = await make_provider()._aprefetch("what does Acme use?")
    assert "Acme uses Postgres." in baseline

    events = []

    async def raising(query, vector_ids, latency_ms):
        events.append("hook-start")
        raise RuntimeError("shadow down")

    p = make_provider(raising)
    out = await p._aprefetch("what does Acme use?")
    events.append("returned")
    assert out == baseline
    assert events == ["returned"]  # hook has not started when prefetch returned
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == ["returned", "hook-start"]


async def test_hook_receives_vector_ids_and_latency_not_text():
    seen = []

    async def hook(query, vector_ids, latency_ms):
        seen.append((query, vector_ids, latency_ms))

    await make_provider(hook)._aprefetch("what does Acme use?")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    (query, vector_ids, latency_ms), = seen
    assert query == "what does Acme use?"
    assert vector_ids == ["conv_7", "conv_8"]
    assert set(latency_ms) >= {"embed", "vector", "total"}


async def test_sync_prefetch_is_unchanged_when_hook_raises_synchronously():
    def sync_boom(*a):
        raise RuntimeError("sync")

    baseline = await make_provider()._aprefetch("q")
    assert await make_provider(sync_boom)._aprefetch("q") == baseline
