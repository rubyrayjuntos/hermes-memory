"""Conversation drain A–F contract against hermes_test (spec §10.2)."""
from __future__ import annotations

import hashlib

import pytest

from hermes_memory.provider import HybridAgeMemoryProvider

pytestmark = [pytest.mark.store, pytest.mark.integration]


TWO_IDS = (
    "RateLimiter talks to nomic-embed-text after we continue working on the drain path."
)
EMPTY_EXTRACT = (
    "yes also just only very much more most some any each every both either "
    "neither for from with without into onto about after before during since "
    "until because so such than too now next first second third last final"
)


def _assert_hermes_test_dsn(dsn: str) -> None:
    assert "hermes_test" in dsn
    assert "hermes_memory" not in dsn


class FakeEmbedder:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = set(fail or ())

    async def embed_text(self, text: str):
        if text == "__fail__" or text in self.fail:
            return None
        h = hashlib.sha256(text.encode("utf-8")).digest()
        scaled = (h[0] % 97) / 97.0
        vec = [0.0] * 768
        vec[0] = scaled if scaled > 0 else 0.01
        vec[1] = ((h[1] % 97) / 97.0) or 0.02
        vec[2] = ((h[2] % 97) / 97.0) or 0.03
        vec[3] = 0.04
        return vec


def _parse_vec(raw) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x.strip()]


def _make_provider(store, embedder) -> HybridAgeMemoryProvider:
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.config = None
    p.pool = store.pool
    p.store = store
    p.embedder = embedder
    p._agent_identity = "default"
    p._session_id = "default"
    p._last_turn_id = {}
    p._concept_emb = {}
    p._concept_names = None
    p._write_queue = None
    p._loop = None
    p._primary_context = True
    p._recent_topics = []
    p._recent_turns = []
    p._max_topics = 8
    p._max_turns = 6
    return p


async def write_turn_item(provider, item: dict) -> None:
    await provider._awrite_item(item)


async def _age_about_count(db_pool, session_id: str) -> int:
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        try:
            row = await conn.fetchrow(
                """
                SELECT * FROM cypher('hermes_knowledge', $$
                    MATCH (t:Turn)-[r:ABOUT]->(c:Concept)
                    WHERE t.session_id = '%s'
                    RETURN count(r)
                $$) AS (c agtype)
                """ % session_id.replace("'", "")
            )
        except Exception:
            return 0
    if row is None:
        return 0
    try:
        return int(str(row["c"]).strip().strip('"') or 0)
    except ValueError:
        return 0


async def _next_count(db_pool, session_id: str) -> int:
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")
        try:
            row = await conn.fetchrow(
                """
                SELECT * FROM cypher('hermes_knowledge', $$
                    MATCH (a:Turn)-[r:NEXT]->(b:Turn)
                    WHERE a.session_id = '%s' OR b.session_id = '%s'
                    RETURN count(r)
                $$) AS (c agtype)
                """ % (session_id.replace("'", ""), session_id.replace("'", ""))
            )
        except Exception:
            return 0
    if row is None:
        return 0
    try:
        return int(str(row["c"]).strip().strip('"') or 0)
    except ValueError:
        return 0


@pytest.mark.asyncio
async def test_two_identifiers_mention_order_chain(
    db_pool, store, clean_hermes_test_db, hermes_test_dsn,
):
    _assert_hermes_test_dsn(hermes_test_dsn)
    embedder = FakeEmbedder()
    provider = _make_provider(store, embedder)
    sid = "sess-two-ids"
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": TWO_IDS,
        "previous_conversation_id": None,
    })
    async with db_pool.acquire() as conn:
        conv = await conn.fetchrow(
            "SELECT id, embedding IS NOT NULL AS has_emb FROM conversations WHERE session_id=$1",
            sid,
        )
        assert conv is not None
        turn_id = int(conv["id"])
        chunk_id = f"conv_{turn_id}"
        passports = await conn.fetch(
            "SELECT chunk_id, source, noun_id, vertex_id FROM memory_chunk_nodes "
            "WHERE turn_id=$1 ORDER BY noun_id",
            turn_id,
        )
        labels = await conn.fetch(
            """
            SELECT n.label, mcn.chunk_id, mcn.source
              FROM memory_chunk_nodes mcn
              JOIN noun n ON n.id = mcn.noun_id
             WHERE mcn.turn_id=$1
             ORDER BY n.id
            """,
            turn_id,
        )
        edges = await conn.fetch(
            """
            SELECT e.src_noun, e.tgt_noun, e.verb_type, e.magnitude, e.e_src_vec,
                   ns.label AS src_label
              FROM semantic_edge e
              JOIN noun ns ON ns.id = e.src_noun
             WHERE e.last_active_turn=$1
             ORDER BY e.id
            """,
            turn_id,
        )
    assert all(p["chunk_id"] == chunk_id for p in passports)
    assert all(p["source"] == "conversation" for p in passports)
    assert all(p["noun_id"] is not None for p in passports)
    assert len(passports) >= 2
    names = [r["label"] for r in labels]
    assert "RateLimiter" in names
    assert "nomic-embed-text" in names
    assert edges, "expected mentions chain"
    assert all(e["verb_type"] == "mentions" for e in edges)
    assert all(float(e["magnitude"]) <= 8.0 for e in edges)
    src_expected = await embedder.embed_text(edges[0]["src_label"])
    assert _parse_vec(edges[0]["e_src_vec"])[:4] == pytest.approx(src_expected[:4], abs=1e-5)
    assert await _age_about_count(db_pool, sid) == 0


@pytest.mark.asyncio
async def test_embed_none_skips_f(db_pool, store, clean_hermes_test_db, hermes_test_dsn):
    _assert_hermes_test_dsn(hermes_test_dsn)
    embedder = FakeEmbedder(fail={TWO_IDS})
    provider = _make_provider(store, embedder)
    sid = "sess-embed-none"
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": TWO_IDS,
        "previous_conversation_id": None,
    })
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, embedding FROM conversations WHERE session_id=$1", sid,
        )
        assert row is not None
        assert row["embedding"] is None
        n_edges = await conn.fetchval(
            "SELECT count(*) FROM semantic_edge WHERE last_active_turn=$1", row["id"],
        )
        n_pass = await conn.fetchval(
            "SELECT count(*) FROM memory_chunk_nodes WHERE turn_id=$1 AND noun_id IS NOT NULL",
            row["id"],
        )
    assert int(n_edges) == 0
    assert int(n_pass) >= 2


@pytest.mark.asyncio
async def test_age_merge_fail_keeps_row_null_vertex(
    db_pool, store, clean_hermes_test_db, hermes_test_dsn, monkeypatch,
):
    _assert_hermes_test_dsn(hermes_test_dsn)

    async def boom(*_a, **_k):
        raise RuntimeError("age merge failed")

    monkeypatch.setattr(store, "merge_vertices_batched", boom)
    provider = _make_provider(store, FakeEmbedder())
    sid = "sess-age-fail"
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": TWO_IDS,
        "previous_conversation_id": None,
    })
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM conversations WHERE session_id=$1", sid)
        assert row is not None
        vids = await conn.fetch(
            "SELECT vertex_id, noun_id FROM memory_chunk_nodes WHERE turn_id=$1",
            row["id"],
        )
    assert vids
    assert all(r["noun_id"] is not None for r in vids)
    assert all(r["vertex_id"] is None for r in vids)


@pytest.mark.asyncio
async def test_src_label_embed_fail_skips_edge(
    db_pool, store, clean_hermes_test_db, hermes_test_dsn,
):
    _assert_hermes_test_dsn(hermes_test_dsn)
    embedder = FakeEmbedder(fail={"RateLimiter"})
    provider = _make_provider(store, embedder)
    sid = "sess-src-fail"
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": TWO_IDS,
        "previous_conversation_id": None,
    })
    async with db_pool.acquire() as conn:
        turn_id = await conn.fetchval("SELECT id FROM conversations WHERE session_id=$1", sid)
        n_pass = await conn.fetchval(
            "SELECT count(*) FROM memory_chunk_nodes WHERE turn_id=$1 AND noun_id IS NOT NULL",
            turn_id,
        )
        n_edges = await conn.fetchval(
            "SELECT count(*) FROM semantic_edge e "
            "JOIN noun n ON n.id = e.src_noun "
            "WHERE e.last_active_turn=$1 AND n.label='RateLimiter'",
            turn_id,
        )
    assert int(n_pass) >= 2
    assert int(n_edges) == 0


@pytest.mark.asyncio
async def test_repeat_pair_caps_magnitude_src_stable(
    db_pool, store, clean_hermes_test_db, hermes_test_dsn,
):
    _assert_hermes_test_dsn(hermes_test_dsn)
    embedder = FakeEmbedder()
    provider = _make_provider(store, embedder)
    sid = "sess-repeat"
    item = {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": TWO_IDS,
        "previous_conversation_id": None,
    }
    await write_turn_item(provider, item)
    async with db_pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT e.magnitude, e.e_src_vec, e.e_tgt_vec, e.src_noun, e.tgt_noun "
            "FROM semantic_edge e ORDER BY e.id LIMIT 1"
        )
    assert first is not None
    src4 = _parse_vec(first["e_src_vec"])[:4]
    tgt4 = _parse_vec(first["e_tgt_vec"])[:4]
    for i in range(12):
        await write_turn_item(provider, {
            **item,
            "content": TWO_IDS + f" repeat-{i}",
            "role": "assistant" if i % 2 else "user",
            "previous_conversation_id": None,
        })
    async with db_pool.acquire() as conn:
        edge = await conn.fetchrow(
            "SELECT magnitude, e_src_vec, e_tgt_vec, provenance_turns "
            "FROM semantic_edge WHERE src_noun=$1 AND tgt_noun=$2",
            first["src_noun"], first["tgt_noun"],
        )
    assert float(edge["magnitude"]) == 8.0
    assert _parse_vec(edge["e_src_vec"])[:4] == pytest.approx(src4, abs=1e-5)
    assert _parse_vec(edge["e_tgt_vec"])[:4] != pytest.approx(tgt4, abs=1e-4)
    assert len(list(edge["provenance_turns"] or [])) <= 32


@pytest.mark.asyncio
async def test_empty_extract_zero_passports(
    db_pool, store, clean_hermes_test_db, hermes_test_dsn,
):
    _assert_hermes_test_dsn(hermes_test_dsn)
    provider = _make_provider(store, FakeEmbedder())
    sid = "sess-empty"
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": EMPTY_EXTRACT,
        "previous_conversation_id": None,
    })
    async with db_pool.acquire() as conn:
        turn_id = await conn.fetchval("SELECT id FROM conversations WHERE session_id=$1", sid)
        assert turn_id is not None
        n_pass = await conn.fetchval(
            "SELECT count(*) FROM memory_chunk_nodes WHERE chunk_id=$1",
            f"conv_{turn_id}",
        )
        n_edges = await conn.fetchval(
            "SELECT count(*) FROM semantic_edge WHERE last_active_turn=$1", turn_id,
        )
    assert int(n_pass) == 0
    assert int(n_edges) == 0
    assert await _age_about_count(db_pool, sid) == 0


@pytest.mark.asyncio
async def test_user_assistant_two_nexts(
    db_pool, store, clean_hermes_test_db, hermes_test_dsn,
):
    _assert_hermes_test_dsn(hermes_test_dsn)
    provider = _make_provider(store, FakeEmbedder())
    sid = "sess-next-pair"
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": TWO_IDS,
        "previous_conversation_id": None,
    })
    first = provider._last_turn_id[sid]
    prior = await store.insert_turn(
        sid, "default", "user", "seed prior turn for two NEXT edges " + "x" * 20,
        None, {"kind": "interactive"},
    )
    # Chain: prior (flower-only id) is not on AGE; use first written turn as previous
    # then user+assistant contracts with chained previous_conversation_id.
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "user",
        "content": TWO_IDS + " user follow-up",
        "previous_conversation_id": first,
    })
    second = provider._last_turn_id[sid]
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": sid,
        "role": "assistant",
        "content": TWO_IDS + " assistant reply",
        "previous_conversation_id": second,
    })
    n_next = await _next_count(db_pool, sid)
    assert n_next >= 2
    assert prior is not None
    assert await _age_about_count(db_pool, sid) == 0


@pytest.mark.asyncio
async def test_sync_turn_stamps_previous_and_two_contracts(store):
    provider = _make_provider(store, FakeEmbedder())
    provider._last_turn_id["sess-q"] = 42
    queued: list[dict] = []

    def capture(item: dict) -> None:
        queued.append(item)

    provider._enqueue_write = capture  # type: ignore[method-assign]
    provider.sync_turn(
        TWO_IDS,
        TWO_IDS + " assistant",
        session_id="sess-q",
    )
    assert len(queued) == 2
    assert queued[0]["previous_conversation_id"] == 42
    assert queued[1]["previous_conversation_id"] == 42
    assert queued[0]["role"] == "user"
    assert queued[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_drain_never_raises_on_insert_fail(store, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(store, "insert_turn", boom)
    provider = _make_provider(store, FakeEmbedder())
    await write_turn_item(provider, {
        "type": "turn",
        "session_id": "sess-b-fail",
        "role": "user",
        "content": TWO_IDS,
        "previous_conversation_id": None,
    })
