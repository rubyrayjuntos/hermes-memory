"""Owner-scoped claim retrieval against the isolated integration database."""
from __future__ import annotations

import pytest

from tests.integration.claims_helpers import span

pytestmark = [pytest.mark.store, pytest.mark.integration]


def _vector() -> str:
    return "[" + ",".join(["1.0"] + ["0.0"] * 767) + "]"


@pytest.mark.asyncio
async def test_claim_retrieval_and_entity_linking_are_owner_scoped(
    db_pool, store, clean_hermes_test_db,
):
    claims_a = await store.record_rule_claims(
        await span(store, "owner-a", 1),
        [{"subject": "Acme", "verb": "uses", "object": "Postgres"}],
    )
    claims_b = await store.record_rule_claims(
        await span(store, "owner-b", 2),
        [{"subject": "Acme", "verb": "uses", "object": "Redis"}],
    )
    claim_a, = claims_a
    claim_b, = claims_b
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE claims SET fact_vec = $1::vector WHERE claim_id = ANY($2::bigint[])",
            _vector(), [claim_a, claim_b],
        )
        subject_a = await conn.fetchval(
            "SELECT subject_id FROM claims WHERE owner = $1 AND claim_id = $2",
            "owner-a", claim_a,
        )
        subject_b = await conn.fetchval(
            "SELECT subject_id FROM claims WHERE owner = $1 AND claim_id = $2",
            "owner-b", claim_b,
        )

    ann_a = await store.claim_vector_search("owner-a", _vector())
    linked_a = await store.link_entities("owner-a", ["acme"], _vector())
    edges_a = await store.claim_edges("owner-a", [subject_a], _vector())
    foreign_claim = await store.claims_by_ids("owner-a", [claim_b])

    assert [int(row["claim_id"]) for row in ann_a] == [claim_a]
    assert [int(row["entity_id"]) for row in linked_a] == [subject_a]
    assert all(int(row["claim_id"]) == claim_a for row in edges_a)
    assert foreign_claim == []
    assert subject_a != subject_b
