"""Integration: concept compaction dedup ordering + orphan prune against hermes_test.

Requires --migrate; skipped otherwise.
"""
from __future__ import annotations

import datetime

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.store]


@pytest.mark.asyncio
async def test_compaction_dedup_ordering_and_weight(db_pool, store, clean_hermes_test_db, hermes_test_dsn):
    assert "hermes_test" in hermes_test_dsn
    # ensure labels
    await store.ensure_about_labels()
    # Clean any leftover Concepts from prior runs (best effort)
    # Fetch and prune orphans? Instead just create fresh Concepts with distinct names
    # Use merge_vertices with weight=1 and created_at now
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    old_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)).isoformat()

    # Create 3 Concepts: two near-duplicate names (we will treat as duplicate via cosine fallback),
    # but for this DB test we directly exercise merge_concept_pair + weight/bridge behavior
    vids = await store.merge_vertices_batched([
        ("Concept", {"name": "Graph Neural Network", "weight": 1, "created_at": now_iso}),
        ("Concept", {"name": "Graph Neural Networks", "weight": 1, "created_at": now_iso}),
        ("Concept", {"name": "Lonely Orphan Concept", "weight": 1, "created_at": old_iso}),
    ])
    assert all(v is not None for v in vids), f"vertex creation failed {vids}"
    c1, c2, orphan = int(vids[0]), int(vids[1]), int(vids[2])

    # Create a Turn and ABOUT edges to c1 and c2 to give them degree=1 (non-orphan)
    t_vids = await store.merge_vertices_batched([
        ("Turn", {"name": f"turn_compaction_{c1}_{c2}", "session_id": "test", "turn_id": 999991, "content": "test"}),
    ])
    assert t_vids[0] is not None
    turn_id = int(t_vids[0])
    done = await store.merge_edges_batched(
        [("ABOUT", turn_id, c1), ("ABOUT", turn_id, c2)],
        edge_props={
            ("ABOUT", turn_id, c1): {"weight": 1.0, "cosine": 0.95},
            ("ABOUT", turn_id, c2): {"weight": 1.0, "cosine": 0.93},
        },
    )
    assert done == 2

    # Bridge rows for Concepts (to test bridge_rows_affected)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name) VALUES "
            "($1, 'test', $2, 'hermes_knowledge'), ($3, 'test', $4, 'hermes_knowledge') ON CONFLICT DO NOTHING",
            f"ctest:{c1}", c1, f"ctest:{c2}", c2,
        )

    # Preview should see bridge rows = 2 for pair (keeper->loser) + orphans
    # Choose keeper = min(c1,c2) deterministic
    keeper, loser = (c1, c2) if c1 < c2 else (c2, c1)
    preview = await store.preview_concept_compaction([(keeper, loser, 0.96)], [orphan])
    assert preview["bridge_rows_affected"] >= 2, preview
    assert len(preview["pairs"]) == 1
    assert preview["pairs"][0]["keeper"] == keeper

    # Merge pair: keeper should gain weight = sum (1+1=2)
    ok = await store.merge_concept_pair(keeper, loser)
    assert ok, "merge_concept_pair failed"

    # Verify loser gone, keeper weight ==2, bridge moved
    concepts_after = await store.fetch_concepts()
    ids_after = {c["id"] for c in concepts_after}
    assert loser not in ids_after, f"loser {loser} should be deleted, got {ids_after}"
    assert keeper in ids_after
    keeper_rec = next(c for c in concepts_after if c["id"] == keeper)
    assert int(keeper_rec["weight"]) == 2, f"keeper weight expected 2, got {keeper_rec}"

    # Bridge row for loser should now point to keeper
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT vertex_id FROM memory_chunk_nodes WHERE chunk_id LIKE 'ctest:%'")
        vids_bridge = {int(r["vertex_id"]) for r in rows}
    assert loser not in vids_bridge
    # At least keeper remains
    assert keeper in vids_bridge

    # Orphan prune: orphan has degree 0 and old created_at -> should be found
    orphan_ids = await store.find_orphan_concept_ids(days=7)
    assert orphan in orphan_ids, f"orphan {orphan} not in {orphan_ids}"

    pruned = await store.prune_orphan_concepts(orphan_ids)
    assert pruned >= 1

    concepts_final = await store.fetch_concepts()
    ids_final = {c["id"] for c in concepts_final}
    assert orphan not in ids_final

    # Cleanup turn
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memory_chunk_nodes WHERE chunk_id LIKE 'ctest:%'")
