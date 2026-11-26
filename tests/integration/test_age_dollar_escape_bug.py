"""Regression test for a dollar-sign escaping bug in age_str() that breaks
real AGE/Cypher vertex MERGE for any content containing a plain '$'.

Found during the LongMemEval benchmark's ingest-timing dry run (2026-11-26):
merge_vertices_batched silently returned None (graph write skipped, logged
only as a warning) for every vertex whose name contained a dollar sign --
not just a pre-existing literal backslash-dollar sequence, but ANY '$'
(e.g. "price is $5", "$HOME", a shell prompt). Root cause: age_str()
escapes '$' as '\\$', but AGE's Cypher string-literal grammar does not
recognize '\\$' as a valid escape sequence (asyncpg raises
InvalidEscapeSequenceError, "Valid escape sequences are \\", \\', \\/, \\\\,
\\b, \\f, \\n, \\r, \\t, \\uXXXX, \\UXXXXXXXX" -- notably NOT '\\$'). The
escaping is also redundant: cypher_dollar_quote()/_pick_cypher_dollar_tag()
already pick a collision-free $tagN$ wrapper for the OUTER dollar-quoted
body, so the inner string literal never needed '$' escaped at all.

See GitHub issue (filed alongside this test) for the proposed fix: drop
the '$' -> '\\$' replacement in age_str() entirely.
"""
from __future__ import annotations

import pytest

from hermes_memory.store import Store


def _assert_hermes_test_dsn(dsn: str) -> None:
    assert "hermes_test" in dsn or "_eval" in dsn, (
        f"refusing to run against non-test DSN: {dsn!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_vertex_with_dollar_sign_in_name_does_not_silently_fail(
    db_pool, store: Store, clean_hermes_test_db, hermes_test_dsn,
):
    """A vertex whose name contains a plain '$' must merge successfully,
    not silently return None while logging a swallowed warning.

    This is a real-content bug, not an edge case: prices ("$5"), shell
    variables ("$HOME"), and any text with a dollar sign all hit it.
    """
    _assert_hermes_test_dsn(hermes_test_dsn)

    results = await store.merge_vertices_batched(
        [("Concept", {"name": "price is $5", "weight": 1.0})]
    )
    assert results == [None], (
        "sanity check failed -- expected this to currently reproduce the "
        "bug (None = silent merge failure); if this now fails, the bug "
        "may already be fixed and this assertion should flip"
    )

    # The actual requirement once fixed: a real vertex id, not None.
    # Left as an explicit xfail-style assertion so CI goes red until the
    # age_str() fix lands, per the GitHub issue.
    assert results[0] is not None, (
        "BUG: merge_vertices_batched silently dropped a vertex whose name "
        "contained a plain '$' character (InvalidEscapeSequenceError from "
        "age_str()'s redundant/broken '$' -> '\\$' escaping). See issue."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_vertex_without_dollar_sign_succeeds_for_contrast(
    db_pool, store: Store, clean_hermes_test_db, hermes_test_dsn,
):
    """Control: a plain name with no '$' merges fine, isolating the bug to
    the dollar-sign escaping path specifically."""
    _assert_hermes_test_dsn(hermes_test_dsn)

    results = await store.merge_vertices_batched(
        [("Concept", {"name": "no dollar here", "weight": 1.0})]
    )
    assert results[0] is not None
