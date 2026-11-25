"""Two owners sharing one session id must never see each other's turns."""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]
SESSION = "default"  # the fallback session id both owners can land in
LONG = "This assistant reply is deliberately longer than eighty characters so dedupe applies to it."


async def _turns(store):
    a1 = await store.insert_turn(SESSION, "alice", "user", "alice asks about her project plan", None)
    b1 = await store.insert_turn(SESSION, "bob", "assistant", "bob's assistant reply about his own work", None)
    a2 = await store.insert_turn(SESSION, "alice", "assistant", "alice's assistant answers her question", None)
    return a1, b1, a2


@pytest.mark.asyncio
async def test_neighbors_stay_within_the_owner(store, clean_hermes_test_db) -> None:
    a1, b1, a2 = await _turns(store)
    nb = await store.conversations_neighbors([a1, a2])
    assert nb[a1]["next"]["turn_id"] == a2
    assert nb[a2]["prev"]["turn_id"] == a1
    for entry in nb.values():
        for side in ("prev", "next"):
            assert (entry.get(side) or {}).get("turn_id") != b1


@pytest.mark.asyncio
async def test_previous_turn_ignores_other_owners(store, clean_hermes_test_db) -> None:
    a1, _b1, a2 = await _turns(store)
    b2 = await store.insert_turn(SESSION, "bob", "assistant", "bob's newest assistant reply", None)
    a3 = await store.insert_turn(SESSION, "alice", "user", "alice follows up", None)
    prior = await store.previous_turn(SESSION, a3, role="assistant")
    assert prior["id"] == a2, f"got {prior['id']}; bob's turn {b2} is newer but belongs to another owner"


@pytest.mark.asyncio
async def test_dedupe_never_returns_another_owners_row(store, clean_hermes_test_db) -> None:
    bob = await store.insert_turn(SESSION, "bob", "assistant", LONG, None)
    alice = await store.insert_turn(SESSION, "alice", "assistant", LONG, None)
    assert alice != bob
    again = await store.insert_turn(SESSION, "alice", "assistant", LONG, None)
    assert again == alice  # same-owner retry still deduplicates


@pytest.mark.asyncio
async def test_conversations_by_ids_filters_by_owner(store, clean_hermes_test_db) -> None:
    a1, b1, _ = await _turns(store)
    rows = await store.conversations_by_ids([a1, b1], agent_identity="alice")
    assert [int(r["id"]) for r in rows] == [a1]
    assert len(await store.conversations_by_ids([a1, b1])) == 2  # operator view


@pytest.mark.asyncio
async def test_memory_entries_by_ids_filters_by_owner(store, clean_hermes_test_db) -> None:
    """PR #117 review: memory_get dispatches mem:<id> pointers here, so the
    lookup must be owner-scoped exactly like vector_search's memory_entries
    branch (and skip file-backed rows, which search never surfaces)."""
    async with store.pool.acquire() as conn:
        a = await conn.fetchval(
            "INSERT INTO memory_entries (agent_identity, target, content) "
            "VALUES ('alice', 'memory', 'alice note') RETURNING id"
        )
        b = await conn.fetchval(
            "INSERT INTO memory_entries (agent_identity, target, content) "
            "VALUES ('bob', 'memory', 'bob note') RETURNING id"
        )
        f = await conn.fetchval(
            "INSERT INTO memory_entries (agent_identity, target, content, metadata) "
            "VALUES ('alice', 'memory', 'alice file row', '{\"file_path\": \"/x\"}') RETURNING id"
        )
    rows = await store.memory_entries_by_ids([a, b, f], agent_identity="alice")
    assert [r["content"] for r in rows] == ["alice note"]


@pytest.mark.asyncio
async def test_doc_chunks_by_ids_returns_only_requested_chunks(store, clean_hermes_test_db) -> None:
    async with store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO doc_chunks (id, doc_hash, source, content) VALUES "
            "('c1', 'h', 's', 'chunk one'), ('c2', 'h', 's2', 'chunk two')"
        )
    rows = await store.doc_chunks_by_ids(["c1", "missing"])
    assert [(r["id"], r["content"]) for r in rows] == [("c1", "chunk one")]
