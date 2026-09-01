"""tests/test_store.py — Store integration tests against isolated hermes_test DB.

Requires DB: run with --migrate. Never touches hermes_memory prod.
Covers insert_turn, upsert/replace/remove memory_entries, bridge symmetry,
vector_search plumbing, and graph expansion savepoint path.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]


def _assert_hermes_test_dsn(dsn: str):
    assert "hermes_test" in dsn, f"must use hermes_test, got {dsn!r}"
    assert "hermes_memory" not in dsn


@pytest.mark.asyncio
async def test_insert_turn_and_vector_search(db_pool, store, clean_hermes_test_db, hermes_test_dsn):
    _assert_hermes_test_dsn(hermes_test_dsn)
    vec = "[" + ",".join(["1.0"] + ["0.0"] * 767) + "]"
    await store.insert_turn("sess-1", "default", "user", "hello store", vec, {"src": "test"})
    rows = await store.vector_search(vec, k=5)
    assert len(rows) >= 1
    assert any(r["content"] == "hello store" for r in rows)


@pytest.mark.asyncio
async def test_upsert_memory_entry_idempotent(db_pool, store, clean_hermes_test_db):
    vec = "[" + ",".join(["0.5"] * 768) + "]"
    await store.upsert_memory_entry("default", "target-a", "content-A", vec, {"k": "v"})
    await store.upsert_memory_entry("default", "target-a", "content-A", vec, {"k": "v"})
    async with db_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM memory_entries WHERE agent_identity=$1 AND target=$2 AND content=$3",
            "default", "target-a", "content-A",
        )
    assert n == 1


@pytest.mark.asyncio
async def test_replace_and_remove_memory_entries(db_pool, store, clean_hermes_test_db):
    vec = "[" + ",".join(["0.3"] * 768) + "]"
    vec2 = "[" + ",".join(["0.9"] * 768) + "]"
    await store.upsert_memory_entry("default", "target-b", "old content hello", vec)
    updated = await store.replace_memory_entries("default", "target-b", "old content", "new content hello", vec2)
    assert updated >= 1
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT content FROM memory_entries WHERE target=$1", "target-b")
    assert any(r["content"] == "new content hello" for r in rows)
    await store.remove_memory_entries("default", "target-b", "new content")
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM memory_entries WHERE target=$1", "target-b")
    assert n == 0


@pytest.mark.asyncio
async def test_bridge_vertex_ids_symmetric(db_pool, store, clean_hermes_test_db):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name) "
            "VALUES ('utest:0', 'doc_chunk', 999991001, 'hermes_knowledge') ON CONFLICT DO NOTHING"
        )
    ids = await store.bridge_vertex_ids(["utest:0"])
    assert ids == ["999991001"]
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memory_chunk_nodes WHERE chunk_id='utest:0'")
    ids = await store.bridge_vertex_ids(["utest:0"])
    assert ids == []
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name) VALUES "
            "('utest:1','doc_chunk', 999991002,'hermes_knowledge'),"
            "('utest:2','doc_chunk', 999991003,'hermes_knowledge') ON CONFLICT DO NOTHING"
        )
    ids = await store.bridge_vertex_ids(["utest:1", "utest:2"])
    assert set(ids) == {"999991002", "999991003"}


@pytest.mark.asyncio
async def test_expand_graph_savepoint_empty(db_pool, store, clean_hermes_test_db):
    rows = await store.expand_graph([], ["Mentions", "Uses"])
    assert rows == []
    rows = await store.expand_graph([999999999], ["Mentions"])
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_safe_label_and_drop_guard(store):
    import pytest as _pytest
    from hermes_memory.store import check_label

    with _pytest.raises(ValueError):
        check_label("bad-label; DROP")
    with _pytest.raises(ValueError):
        check_label("123bad")
    assert check_label("hermes_knowledge") == "hermes_knowledge"

@pytest.mark.asyncio
async def test_expand_graph_weighted_edges(db_pool, store, clean_hermes_test_db):
    """Weighted walk: numeric weight/cosine stored as numerics, ordered by w*c DESC."""
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        try:
            await conn.execute("SELECT create_vlabel('hermes_knowledge', 'TestNode');")
        except Exception:
            pass
        try:
            await conn.execute("SELECT create_elabel('hermes_knowledge', 'RELATED_TO');")
        except Exception:
            pass
    vids = await store.merge_vertices_batched([
        ("TestNode", {"name": "expand_src"}),
        ("TestNode", {"name": "expand_dst_hi"}),
        ("TestNode", {"name": "expand_dst_lo"}),
    ])
    assert all(v is not None for v in vids), f"vertex creation failed: {vids}"
    src_id, hi_id, lo_id = int(vids[0]), int(vids[1]), int(vids[2])

    done = await store.merge_edges_batched(
        [("RELATED_TO", src_id, hi_id), ("RELATED_TO", src_id, lo_id)],
        edge_props={
            ("RELATED_TO", src_id, hi_id): {"weight": 0.9, "cosine": 0.9},
            ("RELATED_TO", src_id, lo_id): {"weight": 0.2, "cosine": 0.3},
        },
    )
    assert done == 2, f"expected 2 edges merged, got {done}"

    rows = await store.expand_graph([src_id])
    assert len(rows) >= 2, f"expected at least 2 rows, got {rows}"
    # agtype rel is quoted '"RELATED_TO"' — strip
    rels = [str(r[1]).strip('"') for r in rows]
    assert "RELATED_TO" in rels, f"no RELATED_TO in {rels} rows={rows}"
    order = [str(r[2]) for r in rows if str(r[1]).strip('"') == "RELATED_TO"]
    hi_pos = next((i for i, s in enumerate(order) if "expand_dst_hi" in s), None)
    lo_pos = next((i for i, s in enumerate(order) if "expand_dst_lo" in s), None)
    assert hi_pos is not None and lo_pos is not None, f"dst not in order={order}"
    assert hi_pos < lo_pos, f"hi {hi_pos} should be before lo {lo_pos} order={order}"
@pytest.mark.asyncio
async def test_about_linker_turn_concept(db_pool, store, clean_hermes_test_db):
    """ABOUT linker: Turn->ABOUT->Concept edges with real cosine, bridge, weighted walk."""
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        for lbl, kind in [("Turn", "v"), ("Concept", "v"), ("ABOUT", "e")]:
            try:
                if kind == "v":
                    await conn.execute(f"SELECT create_vlabel('hermes_knowledge', '{lbl}');")
                else:
                    await conn.execute(f"SELECT create_elabel('hermes_knowledge', '{lbl}');")
            except Exception:
                pass
    # Ensure helper also idempotent
    await store.ensure_about_labels()
    await store.ensure_about_labels()

    # Create Turn vertices and Concept vertices
    vids_turn = await store.merge_vertices_batched([
        ("Turn", {"name": "turn_99901", "session_id": "sess-test", "turn_id": 99901, "content": "hello about linker"}),
    ])
    assert vids_turn[0] is not None, f"Turn vertex failed {vids_turn}"
    turn_id = int(vids_turn[0])

    vids_concept = await store.merge_vertices_batched([
        ("Concept", {"name": "Graph Neural Network"}),
        ("Concept", {"name": "Vector Database"}),
    ])
    assert all(v is not None for v in vids_concept), f"Concept vertices failed {vids_concept}"
    c1, c2 = int(vids_concept[0]), int(vids_concept[1])

    # ABOUT edges with distinct cosine to test ordering + threshold
    done = await store.merge_edges_batched(
        [("ABOUT", turn_id, c1), ("ABOUT", turn_id, c2)],
        edge_props={
            ("ABOUT", turn_id, c1): {"weight": 1.0, "cosine": 0.90},
            ("ABOUT", turn_id, c2): {"weight": 1.0, "cosine": 0.60},
        },
    )
    assert done == 2, f"expected 2 ABOUT edges, got {done}"

    # Bridge row
    await store.bridge_turn(99901, turn_id)
    b = await store.bridge_vertex_ids(["conv_99901"])
    assert str(turn_id) in b, f"bridge missing {b}"

    # Expand: should return Concepts ordered by w*c descending (c1 before c2)
    rows = await store.expand_graph([turn_id])
    # Filter ABOUT rels only
    about_rows = [r for r in rows if str(r[1]).strip('"') == "ABOUT"]
    assert len(about_rows) >= 2, f"expected >=2 ABOUT rows, got {rows}"
    order = [str(r[2]) for r in about_rows]
    hi_pos = next((i for i, s in enumerate(order) if "Graph Neural Network" in s), None)
    lo_pos = next((i for i, s in enumerate(order) if "Vector Database" in s), None)
    assert hi_pos is not None and lo_pos is not None, f"concepts not in order {order}"
    assert hi_pos < lo_pos, f"higher cosine should sort first: hi {hi_pos} lo {lo_pos} order={order}"

    # min_cosine gate: 0.80 should filter out c2 (0.60)
    rows2 = await store.expand_graph([turn_id], min_cosine=0.80)
    about2 = [str(r[2]) for r in rows2 if str(r[1]).strip('"') == "ABOUT"]
    assert any("Graph Neural Network" in s for s in about2), f"c1 should remain {about2}"
    assert not any("Vector Database" in s for s in about2), f"c2 should be filtered {about2}"

