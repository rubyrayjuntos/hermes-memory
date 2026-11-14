# tests/integration/test_claims_aliases.py
"""Alias equations attach surfaces and merge entities (spec §4.1)."""
from __future__ import annotations

import pytest

from tests.integration.claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


@pytest.mark.asyncio
async def test_equation_makes_alias_resolve_to_canon(store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    assert await store.record_alias_equation(s, "Tokyo Eye", "TE")
    assert await store.resolve_entity("alice", "TE") == await store.resolve_entity("alice", "Tokyo Eye")


@pytest.mark.asyncio
async def test_claims_about_alias_land_on_the_canon_entity(db_pool, store, clean_hermes_test_db):
    await store.record_alias_equation(await span(store, "alice", 1), "Tokyo Eye", "TE")
    await store.record_rule_claims(await span(store, "alice", 2), [{"subject": "TE", "verb": "uses", "object": "Atlas"}])
    assert await current(db_pool, "alice", "tokyo eye", "uses") == [("Atlas", "positive")]


@pytest.mark.asyncio
async def test_equation_merges_two_existing_entities_and_their_claims(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [{"subject": "Acme", "verb": "uses", "object": "Atlas"}])
    await store.record_rule_claims(await span(store, "alice", 2), [{"subject": "Acme Corp", "verb": "uses", "object": "Atlas"}])
    eq = await span(store, "alice", 3)
    await store.record_alias_equation(eq, "Acme", "Acme Corp")
    assert await current(db_pool, "alice", "acme", "uses") == [("Atlas", "positive")]
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM claims WHERE owner = 'alice' AND status = 'grounded' AND valid_to IS NULL") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM claim_evidence e JOIN claims c ON c.claim_id = e.claim_id "
            "WHERE c.status = 'grounded' AND c.valid_to IS NULL") == 2


@pytest.mark.asyncio
async def test_abstained_or_assistant_equations_are_ignored(store, clean_hermes_test_db):
    assert not await store.record_alias_equation(await span(store, "alice", 1), "the project", "TP")
    assert not await store.record_alias_equation(await span(store, "alice", 2, role="assistant"), "Acme", "AC")


@pytest.mark.asyncio
async def test_equations_never_cross_owners(store, clean_hermes_test_db):
    await store.record_alias_equation(await span(store, "alice", 1), "Tokyo Eye", "TE")
    assert await store.resolve_entity("bob", "TE") is None
