"""Durable write outbox: writes survive a full queue, a shutdown, or a crash."""

import asyncio

from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider


class FakeStore:
    def __init__(self, stale=None):
        self.added, self.done, self.unclaimed = [], [], []
        self.stale = list(stale or [])

    async def outbox_add(self, owner, item, claimed_by):
        self.added.append((owner, item, claimed_by))
        return 100 + len(self.added)

    async def outbox_done(self, outbox_id):
        self.done.append(outbox_id)

    async def outbox_unclaim(self, outbox_id, error=None):
        self.unclaimed.append((outbox_id, error))

    async def outbox_claim_stale(self, claimed_by, limit, **_):
        rows, self.stale = self.stale[:limit], self.stale[limit:]
        return rows


def _provider(store, maxsize=4):
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.store = store
    p.config = HybridAgeConfig(embed_dim=2, queue_maxsize=maxsize)
    p._agent_identity = "alice"
    p._outbox_owner = "host:1:abc"
    p._loop = None
    p._write_queue = asyncio.Queue(maxsize=maxsize)
    return p


def test_record_attaches_outbox_id_and_never_persists_private_keys():
    store = FakeStore()
    p = _provider(store)
    item = {"type": "turn", "role": "user", "content": "hello there", "_ledger_failed": True}
    p._record_outbox(item)
    assert item["_outbox_id"] == 101
    owner, payload, claimed_by = store.added[0]
    assert owner == "alice" and claimed_by == "host:1:abc"
    assert "_ledger_failed" not in payload and payload["content"] == "hello there"


async def test_settle_deletes_persisted_and_hands_back_failed_writes():
    store = FakeStore()
    p = _provider(store)
    await p._settle_outbox({"_outbox_id": 7, "_persisted": True})
    await p._settle_outbox({"_outbox_id": 8})
    await p._settle_outbox({"type": "turn"})  # no outbox id: nothing to settle
    assert store.done == [7]
    assert store.unclaimed == [(8, "write not persisted")]


async def test_replay_queues_claimed_rows_up_to_free_space():
    rows = [(i, {"type": "turn", "content": f"t{i}"}) for i in range(1, 7)]
    store = FakeStore(stale=rows)
    p = _provider(store, maxsize=4)
    await p._write_queue.put({"type": "turn", "content": "already queued"})
    queued = await p._replay_outbox()
    assert queued == 3  # 4 slots, 1 taken
    replayed = [p._write_queue.get_nowait() for _ in range(4)][1:]
    assert [r["_outbox_id"] for r in replayed] == [1, 2, 3]
    assert len(store.stale) == 3  # left for the next replay pass, still durable


async def test_replayed_turn_finds_its_previous_turn_in_the_database():
    class Store:
        async def latest_turn_id(self, owner, session_id):
            assert (owner, session_id) == ("alice", "s1")
            return 40

    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p._last_turn_id, p._agent_identity = {}, "alice"
    assert await p._resolve_previous_turn(Store(), "s1", "alice") == 40
    p._last_turn_id = {"s1": 55}
    assert await p._resolve_previous_turn(Store(), "s1", "alice") == 55
