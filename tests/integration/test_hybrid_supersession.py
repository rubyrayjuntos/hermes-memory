"""NYC -> Austin -> NYC must leave only the latest NYC claim current and injected."""
from __future__ import annotations

import pytest

from hermes_memory.embed import vec_to_literal
from hermes_memory.provider_helpers import format_facts
from hermes_memory.retrieve_hybrid import retrieve_hybrid
from hermes_memory.store_claims import write_fact_vectors
from tests.integration.claims_helpers import current, history, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


def _vec(seed: str) -> list[float]:
    values = [((ord(ch) % 97) / 97.0) or 0.01 for ch in (seed * 96)[:768]]
    return values[:768] + [0.01] * max(0, 768 - len(values))


async def _embed_current(db_pool, owner: str) -> None:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT claim_id, fact_text FROM claims "
            "WHERE owner = $1 AND status = 'grounded' AND valid_to IS NULL AND fact_text IS NOT NULL",
            owner,
        )
        await write_fact_vectors(
            conn,
            [(int(row["claim_id"]), _vec(str(row["fact_text"]))) for row in rows],
            "nomic-embed-text",
        )


@pytest.mark.asyncio
async def test_is_city_timeline_only_latest_nyc_is_current(db_pool, store, clean_hermes_test_db):
    for minute, city in [(1, "NYC"), (2, "Austin"), (3, "NYC")]:
        await store.record_rule_claims(
            await span(store, "alice", minute),
            [{"subject": "Home", "verb": "is", "object": city}],
        )
    assert await history(db_pool, "alice", "home", "is") == [
        ("NYC", "grounded", False),
        ("Austin", "grounded", False),
        ("NYC", "grounded", True),
    ]
    assert await current(db_pool, "alice", "home", "is") == [("NYC", "positive")]


@pytest.mark.asyncio
async def test_hybrid_injects_current_nyc_not_austin(db_pool, store, clean_hermes_test_db):
    for minute, city in [(1, "NYC"), (2, "Austin"), (3, "NYC")]:
        await store.record_rule_claims(
            await span(store, "alice", minute),
            [{"subject": "Home", "verb": "is", "object": city}],
        )
    await _embed_current(db_pool, "alice")
    query = "Where is my city now?"
    vec = vec_to_literal(_vec(query))
    result = await retrieve_hybrid(store, "alice", query, vec, [])
    block = format_facts(result["facts"])
    texts = [str(fact.get("fact_text") or "") for fact in result["facts"]]
    assert any("NYC" in text for text in texts), result
    assert all("Austin" not in text for text in texts), result
    assert "NYC" in block
    assert "Austin" not in block
    statuses = {str(fact.get("status")) for fact in result["facts"]}
    assert statuses <= {"grounded"}
