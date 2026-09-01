"""Integration: recency decay ordering in expand_graph (issue #38).

Requires --migrate; skipped otherwise.
Same w×c, newer edge (edge created_at) must rank trước legacy/older.
Also verifies edge fallback to vertex created_at and legacy fallback 0.5.
"""
from __future__ import annotations

import datetime
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.store]


@pytest.mark.asyncio
async def test_expand_graph_recency_decay(db_pool, store, clean_hermes_test_db, hermes_test_dsn):
    assert "hermes_test" in hermes_test_dsn
    await store.ensure_about_labels()
    # ensure extra test labels exist
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        for lbl, kind in [("RecencySrc", "v"), ("RecencyDst", "v"), ("ABOUT", "e")]:
            try:
                if kind == "v":
                    await conn.execute(f"SELECT create_vlabel('hermes_knowledge', '{lbl}');")
                else:
                    await conn.execute(f"SELECT create_elabel('hermes_knowledge', '{lbl}');")
            except Exception:
                pass

    now = datetime.datetime.now(datetime.timezone.utc)
    recent_iso = now.isoformat()
    old_iso = (now - datetime.timedelta(days=60)).isoformat()

    # Create src + two dst vertices with identical weight but different created_at
    # Dst vertices themselves have distinct created_at; edges carry created_at.
    vids = await store.merge_vertices_batched([
        ("RecencySrc", {"name": "recency_src"}),
        ("RecencyDst", {"name": "recency_dst_new", "weight": 1, "created_at": recent_iso}),
        ("RecencyDst", {"name": "recency_dst_old", "weight": 1, "created_at": old_iso}),
        ("RecencyDst", {"name": "recency_dst_legacy", "weight": 1}),
    ])
    assert all(v is not None for v in vids), f"vertex creation failed {vids}"
    src_id, new_id, old_id, legacy_id = map(int, vids)

    # Edges from src to each dst with SAME weight+cosine but different recency
    done = await store.merge_edges_batched(
        [("ABOUT", src_id, new_id), ("ABOUT", src_id, old_id), ("ABOUT", src_id, legacy_id)],
        edge_props={
            ("ABOUT", src_id, new_id): {"weight": 0.8, "cosine": 0.85, "created_at": recent_iso},
            ("ABOUT", src_id, old_id): {"weight": 0.8, "cosine": 0.85, "created_at": old_iso},
            # legacy: no created_at -> fallback 0.5
            ("ABOUT", src_id, legacy_id): {"weight": 0.8, "cosine": 0.85},
        },
    )
    assert done == 3, f"expected 3 edges, got {done}"

    rows = await store.expand_graph([src_id], limit=10)
    # filter only ABOUT edges to these dsts (expand_graph may also return src self row if no edge? but we check ordering)
    about_rows = [r for r in rows if str(r[1]).strip('"') == "ABOUT"]
    assert len(about_rows) >= 3, f"expected >=3 ABOUT rows, got {rows}"
    order_str = [str(r[2]) for r in about_rows]
    # Find positions
    pos_new = next((i for i, s in enumerate(order_str) if "recency_dst_new" in s), None)
    pos_legacy = next((i for i, s in enumerate(order_str) if "recency_dst_legacy" in s), None)
    pos_old = next((i for i, s in enumerate(order_str) if "recency_dst_old" in s), None)
    assert pos_new is not None and pos_old is not None and pos_legacy is not None, f"dst missing in {order_str}"
    # Newest must be first, oldest last, legacy in middle (decay 0.5 > exp(-60/30)=0.135)
    assert pos_new < pos_legacy < pos_old, f"recency ordering failed new:{pos_new} legacy:{pos_legacy} old:{pos_old} order={order_str}"


@pytest.mark.asyncio
async def test_expand_graph_vertex_fallback(db_pool, store, clean_hermes_test_db):
    """Edge without created_at falls back to target vertex created_at."""
    await store.ensure_about_labels()
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        for lbl, kind in [("RecencySrc2", "v"), ("RecencyDst2", "v"), ("ABOUT", "e")]:
            try:
                if kind == "v":
                    await conn.execute(f"SELECT create_vlabel('hermes_knowledge', '{lbl}');")
                else:
                    await conn.execute(f"SELECT create_elabel('hermes_knowledge', '{lbl}');")
            except Exception:
                pass

    now = datetime.datetime.now(datetime.timezone.utc)
    recent_iso = now.isoformat()
    old_iso = (now - datetime.timedelta(days=60)).isoformat()

    vids = await store.merge_vertices_batched([
        ("RecencySrc2", {"name": "recency_src2"}),
        ("RecencyDst2", {"name": "recency_dst2_newV", "weight": 1, "created_at": recent_iso}),
        ("RecencyDst2", {"name": "recency_dst2_oldV", "weight": 1, "created_at": old_iso}),
    ])
    src_id, newV_id, oldV_id = map(int, vids)

    # Edges WITHOUT created_at -> should fallback to vertex created_at
    await store.merge_edges_batched(
        [("ABOUT", src_id, newV_id), ("ABOUT", src_id, oldV_id)],
        edge_props={
            ("ABOUT", src_id, newV_id): {"weight": 0.7, "cosine": 0.8},
            ("ABOUT", src_id, oldV_id): {"weight": 0.7, "cosine": 0.8},
        },
    )
    rows = await store.expand_graph([src_id], limit=10)
    about = [str(r[2]) for r in rows if str(r[1]).strip('"') == "ABOUT"]
    pos_new = next((i for i, s in enumerate(about) if "recency_dst2_newV" in s), None)
    pos_old = next((i for i, s in enumerate(about) if "recency_dst2_oldV" in s), None)
    assert pos_new is not None and pos_old is not None, f"missing {about}"
    assert pos_new < pos_old, f"vertex fallback recency failed new:{pos_new} old:{pos_old} {about}"
