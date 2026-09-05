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
    assert all(r["embedding"] for r in rows)
    async with db_pool.acquire() as conn:
        stamped = await conn.fetchrow(
            "SELECT embed_model, embed_dim FROM conversations WHERE content = $1",
            "hello store",
        )
    assert stamped["embed_model"] == "nomic-embed-text"
    assert int(stamped["embed_dim"]) == 768


@pytest.mark.asyncio
async def test_require_schema_head_passes_after_migrate(store, hermes_test_dsn):
    _assert_hermes_test_dsn(hermes_test_dsn)
    await store.require_schema_head()


@pytest.mark.asyncio
async def test_require_embed_version_columns_passes_after_v10(store, hermes_test_dsn):
    _assert_hermes_test_dsn(hermes_test_dsn)
    await store.require_embed_version_columns()


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
    q_vec = [1.0] + [0.0] * 767
    rows = await store.expand_graph([], q_vec=q_vec)
    assert rows == []
    rows = await store.expand_graph([999999999], q_vec=q_vec)
    assert rows == []


@pytest.mark.asyncio
async def test_safe_label_and_drop_guard(store):
    import pytest as _pytest
    from hermes_memory.store import check_label

    with _pytest.raises(ValueError):
        check_label("bad-label; DROP")
    with _pytest.raises(ValueError):
        check_label("123bad")
    assert check_label("hermes_knowledge") == "hermes_knowledge"

