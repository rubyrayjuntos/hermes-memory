"""Issue #123: Store.fetch_noun_labels_for_slugs() must stay index-backed,
not a full `noun` table scan, as the table grows. Against hermes_test."""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]


@pytest.mark.asyncio
async def test_fetch_noun_labels_for_slugs_returns_only_matching_rows(
    db_pool, store, clean_hermes_test_db,
) -> None:
    await store.upsert_noun("RateLimiter", "tool")
    await store.upsert_noun("nomic-embed-text", "technology")
    # 10,000 unrelated rows -- the whole point of the fix is that these never
    # need to be read for a lookup that only cares about a couple of slugs.
    async with db_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO noun (label) VALUES ($1) ON CONFLICT (label) DO NOTHING",
            [(f"unrelated-noun-{i}",) for i in range(10_000)],
        )

    from hermes_memory.extract_nouns import _slug

    wanted = [_slug("RateLimiter"), _slug("nomic-embed-text"), _slug("totally-absent")]
    result = await store.fetch_noun_labels_for_slugs(wanted)

    assert set(result) == {"RateLimiter", "nomic-embed-text"}
    assert not any(r.startswith("unrelated-noun-") for r in result)


@pytest.mark.asyncio
async def test_fetch_noun_labels_for_slugs_empty_input_returns_empty(
    db_pool, store, clean_hermes_test_db,
) -> None:
    await store.upsert_noun("SomeLabel", None)
    assert await store.fetch_noun_labels_for_slugs([]) == []


@pytest.mark.asyncio
async def test_fetch_noun_labels_for_slugs_uses_an_index_not_a_seq_scan(
    db_pool, store, clean_hermes_test_db,
) -> None:
    """The literal point of #123: with many rows in `noun`, the per-turn
    lookup must not plan a sequential scan over the whole table."""
    await store.upsert_noun("RateLimiter", "tool")
    async with db_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO noun (label) VALUES ($1) ON CONFLICT (label) DO NOTHING",
            [(f"unrelated-noun-{i}",) for i in range(10_000)],
        )
        await conn.execute("ANALYZE noun")
        plan = await conn.fetchval(
            "EXPLAIN (FORMAT JSON) SELECT label FROM noun WHERE slug = ANY($1::text[])",
            ["ratelimiter"],
        )

    import json

    plan_text = json.dumps(plan)
    assert "Seq Scan" not in plan_text, f"expected an index-backed plan, got: {plan_text}"
