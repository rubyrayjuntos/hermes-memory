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


@pytest.mark.asyncio
async def test_v24_migration_backfills_slug_for_preexisting_noun_rows(
    db_pool, store, clean_hermes_test_db,
) -> None:
    """Reviewer ask: the migration must backfill `slug` for rows that already
    existed on an already-installed database, not just new writes -- or
    fetch_noun_labels_for_slugs() silently misses every old noun."""
    import pathlib

    # Simulate pre-existing data written before the `slug` column existed:
    # a bare INSERT with no slug, exactly what rows written under the old
    # schema (pre-V24) would look like.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO noun (label) VALUES ($1), ($2) ON CONFLICT (label) DO NOTHING",
            "RateLimiter", "AT&T",
        )
        # Roll the schema back to "V24 has never run" while keeping those
        # pre-existing rows, then re-apply the real migration file text --
        # not a re-derivation of it -- exactly as an upgrade would.
        await conn.execute("DROP INDEX IF EXISTS idx_noun_slug")
        await conn.execute("ALTER TABLE noun DROP COLUMN IF EXISTS slug")

        migration_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "sql" / "migrations" / "V24__noun_label_slug_index.sql"
        )
        await conn.execute(migration_path.read_text())

        rows = await conn.fetch(
            "SELECT label, slug FROM noun WHERE label IN ('RateLimiter', 'AT&T')"
        )
    by_label = {r["label"]: r["slug"] for r in rows}
    assert by_label["RateLimiter"] == "ratelimiter"
    assert by_label["AT&T"] == "at-t"


@pytest.mark.asyncio
async def test_upsert_noun_slug_matches_python_slug_for_unicode_and_case_folding(
    db_pool, store, clean_hermes_test_db,
) -> None:
    """Reviewer ask: Python's _slug() and the stored slug must agree on
    non-ASCII labels and tricky ASCII case-folding, not just plain English
    words. upsert_noun() writes slug in Python (not a SQL-generated column)
    precisely so this holds by construction, including cases where Python's
    str.lower() and Postgres's lower() diverge -- e.g. Turkish dotted capital
    I (U+0130), which Python expands to two codepoints ('i' + a combining
    dot above) and Postgres collapses to a plain ASCII 'i'."""
    from hermes_memory.extract_nouns import _slug

    labels = [
        "CAFÉ", "ÀÉÎÕÜ", "ПРИВЕТ", "İstanbul", "STRASSE", "ß",
        "Ａ full width", "Ñoño", "RateLimiter", "AT&T",
    ]
    async with db_pool.acquire() as conn:
        for label in labels:
            await store.upsert_noun(label, None)
        rows = await conn.fetch(
            "SELECT label, slug FROM noun WHERE label = ANY($1::text[])", labels
        )
    by_label = {r["label"]: r["slug"] for r in rows}
    for label in labels:
        assert by_label[label] == _slug(label), (
            f"stored slug for {label!r} ({by_label[label]!r}) does not match "
            f"the Python-computed slug ({_slug(label)!r})"
        )
    # The specific divergence this test exists to catch: if upsert_noun ever
    # regresses to a SQL-generated column, this is the case that would break.
    assert _slug("İstanbul") == "i-stanbul"
