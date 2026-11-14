"""Durable write outbox: idempotent by op_id, fenced settles, replay-aware ordering."""

import asyncio

from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider


class FakeStore:
    def __init__(self, stale=None, add_result="new"):
        self.added, self.done, self.unclaimed = [], [], []
        self.stale = list(stale or [])
        self.add_result = add_result

    async def outbox_add(self, owner, item, claimed_by, op_id):
        self.added.append((owner, item, claimed_by, op_id))
        return None if self.add_result is None else (100 + len(self.added), 1)

    async def outbox_done(self, outbox_id, fence):
        self.done.append((outbox_id, fence))
        return True

    async def outbox_unclaim(self, outbox_id, fence, error=None):
        self.unclaimed.append((outbox_id, fence, error))
        return True

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


def test_record_sends_op_id_and_attaches_id_and_fence_without_private_keys():
    store = FakeStore()
    p = _provider(store)
    item = {"type": "turn", "content": "hello there", "op_id": "op-1", "captured_at": "2026-11-14T00:00:00+00:00",
            "_ledger_failed": True}
    p._record_outbox(item)
    assert (item["_outbox_id"], item["_fence"]) == (101, 1)
    owner, payload, claimed_by, op_id = store.added[0]
    assert (owner, claimed_by, op_id) == ("alice", "host:1:abc", "op-1")
    assert "_ledger_failed" not in payload and payload["captured_at"] == "2026-11-14T00:00:00+00:00"


def test_record_of_an_already_settled_op_attaches_nothing():
    p = _provider(FakeStore(add_result=None))
    item = {"type": "turn", "content": "x", "op_id": "op-1"}
    p._record_outbox(item)
    assert "_outbox_id" not in item


async def test_settle_is_fenced():
    store = FakeStore()
    p = _provider(store)
    await p._settle_outbox({"_outbox_id": 7, "_fence": 3, "_persisted": True})
    await p._settle_outbox({"_outbox_id": 8, "_fence": 2})
    await p._settle_outbox({"type": "turn"})  # never recorded: nothing to settle
    assert store.done == [(7, 3)]
    assert store.unclaimed == [(8, 2, "write not persisted")]


async def test_replay_marks_items_replayed_with_their_fence_up_to_free_space():
    rows = [(i, 10 + i, {"type": "turn", "content": f"t{i}", "op_id": f"op-{i}"}) for i in range(1, 7)]
    store = FakeStore(stale=rows)
    p = _provider(store, maxsize=4)
    await p._write_queue.put({"type": "turn", "content": "already queued"})
    assert await p._replay_outbox() == 3
    replayed = [p._write_queue.get_nowait() for _ in range(4)][1:]
    assert [(r["_outbox_id"], r["_fence"], r["_replayed"]) for r in replayed] == [
        (1, 11, True), (2, 12, True), (3, 13, True)]


class PrevStore:
    def __init__(self):
        self.calls = []

    async def previous_turn_before(self, owner, session_id, ts):
        self.calls.append((owner, session_id, ts))
        return 40


async def test_live_item_uses_the_in_memory_previous_turn():
    p = _provider(FakeStore())
    p._last_turn_id = {"s1": 55}
    store = PrevStore()
    assert await p._resolve_previous_turn(store, {"captured_at": "t"}, "s1", "alice") == 55
    assert store.calls == []


async def test_replayed_item_ignores_newer_in_memory_turn_and_uses_its_capture_time():
    """An older turn replayed after newer ones landed must link to its own predecessor."""
    p = _provider(FakeStore())
    p._last_turn_id = {"s1": 99}  # a newer live turn
    store = PrevStore()
    item = {"_replayed": True, "captured_at": "2026-11-14T00:00:00+00:00"}
    assert await p._resolve_previous_turn(store, item, "s1", "alice") == 40
    assert store.calls == [("alice", "s1", "2026-11-14T00:00:00+00:00")]
