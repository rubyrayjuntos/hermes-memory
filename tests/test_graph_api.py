"""Unit tests for the viz API — no database required."""
from __future__ import annotations

import json

import pytest

import hermes_memory.graph_api as graph_api
from hermes_memory.graph_api import (
    GHOST_MAX_K,
    GHOST_MAX_LIMIT,
    catalog_where_clause,
    classify_session_kind,
    clamp_limit,
    conversation_first_budget,
    human_turn_title,
    is_synthetic_session,
    is_verify_session,
    match_route,
    pack_search,
    parse_vertex,
    preview_embedding,
    safe_label,
    stringify_id,
    undirected_knn_edges,
    unpack_expand_row,
    validate_bind_host,
)
from hermes_memory.store import bridge_keys_for_seed


def test_stringify_id_preserves_bigint_decimal():
    assert stringify_id(10133099161583617) == "10133099161583617"
    assert stringify_id("10133099161583617") == "10133099161583617"
    assert stringify_id({"id": "10133099161583617"}) == "10133099161583617"
    dumped = json.dumps({"id": stringify_id(10133099161583617)})
    assert "10133099161583617" in dumped
    assert json.loads(dumped)["id"] == "10133099161583617"


def test_stringify_id_rejects_junk():
    with pytest.raises(ValueError):
        stringify_id(None)
    with pytest.raises(ValueError):
        stringify_id("not-an-id")
    with pytest.raises(ValueError):
        stringify_id(True)


def test_parse_vertex_strips_age_suffix():
    raw = '{"id": 844424930131969, "label": "Turn", "properties": {"name": "hello"}}::vertex'
    v = parse_vertex(raw)
    assert v is not None
    assert v["id"] == "844424930131969"
    assert v["label"] == "Turn"
    assert v["name"] == "hello"


def test_parse_vertex_file_path_name():
    v = parse_vertex(
        {"id": 1, "label": "File", "properties": {"path": "src/hermes_memory/store.py"}}
    )
    assert v is not None
    assert v["name"] == "src/hermes_memory/store.py"


def test_safe_label_all_and_injection():
    assert safe_label("") is None
    assert safe_label("all") is None
    assert safe_label("Turn") == "Turn"
    with pytest.raises(ValueError):
        safe_label("Turn} DETACH DELETE n //")


def test_clamp_limit():
    assert clamp_limit("250") == 250
    assert clamp_limit("99999") == 2000
    assert clamp_limit("nope") == 250
    assert GHOST_MAX_K == 8
    assert GHOST_MAX_LIMIT == 250


def test_bridge_keys_for_seed_are_namespaced():
    assert bridge_keys_for_seed({"id": "42", "src": "conversation"}) == ["conv_42"]
    assert "42" not in bridge_keys_for_seed({"id": "42", "src": "conversation"})
    assert bridge_keys_for_seed({"id": "42", "src": "memory_entry"}) == ["42", "mem_42"]
    assert "conv_42" not in bridge_keys_for_seed({"id": "42", "src": "memory_entry"})
    assert bridge_keys_for_seed({"id": "ab12cd34:0", "src": "doc_chunk"}) == ["ab12cd34:0"]


def test_preview_embedding():
    preview, stats = preview_embedding("[0.1, 0.5, -0.2]", n=2)
    assert preview == [0.1, 0.5]
    assert stats["min"] == -0.2
    assert stats["max"] == 0.5


def test_classify_session_kind():
    assert classify_session_kind("20260902_094854_3c01ca") == "interactive"
    assert classify_session_kind("bench-throughput-1") == "benchmark"
    assert classify_session_kind("verify-c5-verify-1") == "system_test"
    assert classify_session_kind("x", explicit="benchmark") == "benchmark"
    assert classify_session_kind("x", explicit="nope") == "interactive"


def test_human_turn_title():
    assert human_turn_title("You're right. That hub is verify.", "turn_1") == "You're right. That hub is verify."
    assert "turn_472" not in human_turn_title("**Hello** world\nmore", "turn_472")
    assert human_turn_title("", "turn_9") == "turn_9"


def test_humanize_node_turn():
    from hermes_memory.graph_api import humanize_node
    n = humanize_node({
        "id": "1", "label": "Turn", "name": "turn_9",
        "props": {"content": "<img src=x onerror=alert(1)>\nmore", "session_id": "s"},
    })
    assert n is not None
    assert n["name"].startswith("<img")
    assert "snippet" in n
    file_n = humanize_node({"id": "2", "label": "File", "name": "a.py", "props": {}})
    assert file_n is not None
    assert file_n["name"] == "a.py"


def test_is_synthetic_session():
    assert is_synthetic_session("bench-throughput-1788250950")
    assert is_synthetic_session("verify-c5-verify-1")
    assert not is_synthetic_session("20260902_094854_3c01ca")


def test_catalog_where_clause_turn_only():
    w = catalog_where_clause("Turn")
    assert w.startswith("WHERE NOT")
    assert "bench-" in w
    assert "bench_" in w
    assert "verify-c5" in w
    assert "c8-" in w
    assert catalog_where_clause("File") == ""
    assert catalog_where_clause(None) == ""


def test_is_verify_session():
    assert is_verify_session("verify-c5-verify-1788362894")
    assert is_verify_session('"verify-c5-verify-1"')
    assert not is_verify_session("20260902_094854_3c01")
    assert not is_verify_session("")
    assert not is_verify_session(None)


def test_conversation_first_budget():
    convo, other = conversation_first_budget(250)
    assert convo == 175
    assert other == 75
    assert convo > other
    assert convo + other == 250


def test_catalog_assembler_emits_nouns_not_flower():
    assemble_catalog = getattr(graph_api, "assemble_catalog", lambda *_args, **_kwargs: {})
    out = assemble_catalog(
        age_nodes=[
            {"id": "1", "label": "Session", "name": "session-a", "props": {}},
            {"id": "2", "label": "Turn", "name": "turn-a", "props": {"turn_id": 41}},
            {"id": "3", "label": "Concept", "name": "legacy", "props": {}},
        ],
        age_links=[
            {"source": "2", "target": "1", "label": "IN_SESSION", "weight": 1.0, "cosine": 1.0},
            {"source": "2", "target": "3", "label": "ABOUT", "weight": 1.0, "cosine": 0.8},
        ],
        passports=[
            {
                "noun_id": 1, "label": "Postgres", "type": "technology",
                "vertex_id": 2, "turn_id": 41, "session_id": "session-a",
            },
            {"noun_id": 2, "label": "AGE", "type": "technology", "vertex_id": 2, "turn_id": 41},
        ],
        mentions=[
            {
                "src_noun": 1, "tgt_noun": 2, "src_label": "Postgres", "tgt_label": "AGE",
                "magnitude": 3.0, "cosine": 0.82, "decay": 0.9, "score": 0.34,
            },
            {"src_noun": 2, "tgt_noun": 99, "magnitude": 2.0, "decay": 0.8, "score": 0.2},
        ],
        limit=80,
    )

    assert {node["id"] for node in out.get("nodes", [])} == {"noun:1", "noun:2", "noun:99"}
    assert {link["label"] for link in out.get("links", [])} == {"mentions"}
    mention = next(link for link in out["links"] if link["source"] == "noun:1")
    assert mention["cosine"] == 0.82
    assert mention["weight"] == 3.0
    noun = next(node for node in out["nodes"] if node["id"] == "noun:1")
    assert noun["props"]["turn_id"] == 41
    assert noun["props"]["session_id"] == "session-a"
    assert noun["props"]["turn_vertex_id"] == "age:2"
    assert out["graph"]["nodes"] == out["nodes"]
    assert out["meta"]["scope"] == "manifold"


def test_attach_passport_anchors_stamps_provenance_without_flower_gems():
    packed = pack_search(
        "postgres",
        4,
        2,
        [{"id": "41", "content": "noun seed", "similarity": 0.8, "src": "conversation"}],
        ["1"],
        [
            (
                {"id": "1", "name": "Postgres", "label": "Noun"},
                "mentions",
                {"id": "2", "name": "AGE", "label": "Noun"},
                2.0,
                0.8,
                1.0,
                0.6,
                1,
            ),
        ],
    )
    out = graph_api.attach_passport_anchors(
        packed,
        [
            {
                "noun_id": 1,
                "label": "Postgres",
                "type": "technology",
                "vertex_id": 99,
                "turn_id": 41,
                "session_id": "session-a",
            },
            {
                "noun_id": 2,
                "label": "AGE",
                "vertex_id": 99,
                "turn_id": 41,
            },
        ],
    )

    ids = {node["id"] for node in out["graph"]["nodes"]}
    assert "age:99" not in ids
    postgres = next(node for node in out["graph"]["nodes"] if node["id"] == "noun:1")
    assert postgres["name"] == "Postgres"
    assert postgres["props"]["turn_vertex_id"] == "age:99"
    assert postgres["props"]["turn_id"] == 41
    assert postgres["props"]["session_id"] == "session-a"


@pytest.mark.asyncio
async def test_default_graph_catalog_uses_pack_search_manifold():
    from hermes_memory.graph_api import Runtime

    class Acquire:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    class Store:
        graph_name = "hermes_knowledge"

        async def load_age(self, _conn):
            return None

    runtime = Runtime.__new__(Runtime)
    runtime.pool = Pool()
    runtime.store = Store()
    catalog_limits = []
    legacy_calls = []

    async def fake_catalog_data(_conn, _graph, limit):
        catalog_limits.append(limit)
        return (
            [
                {"id": "1", "label": "Session", "name": "session-a", "props": {}},
                {"id": "2", "label": "Turn", "name": "turn-a", "props": {"turn_id": 41}},
            ],
            [{"source": "2", "target": "1", "label": "IN_SESSION", "weight": 1.0, "cosine": 1.0}],
            [{"noun_id": 1, "label": "Postgres", "type": "technology", "vertex_id": 2, "turn_id": 41}],
            [],
        )

    async def fake_legacy_rows(_conn, _graph, label, limit):
        legacy_calls.append((label, limit))
        return []

    runtime._fetch_catalog_data = fake_catalog_data
    runtime._fetch_3d_rows = fake_legacy_rows

    out = await runtime._agraph_3d(None, 250)

    assert catalog_limits == [250]
    assert legacy_calls == []
    assert {node["id"] for node in out["nodes"]} == {"noun:1"}
    assert out["graph"]["nodes"] == out["nodes"]


@pytest.mark.asyncio
async def test_stats_reports_sql_turns_nouns_and_mentions():
    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.graph_api import Runtime

    class Conn:
        async def fetchval(self, query):
            normalized = " ".join(query.split())
            if "FROM conversations" in normalized:
                return 7
            if "FROM noun" in normalized:
                return 5
            if "FROM semantic_edge" in normalized:
                return 3
            return 0

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    class Store:
        graph_name = "hermes_knowledge"

        async def librarian_health(self):
            return {}

        async def load_age(self, _conn):
            return None

    runtime = Runtime.__new__(Runtime)
    runtime.pool = Pool()
    runtime.store = Store()
    runtime.cfg = HybridAgeConfig()

    async def count_cypher(_conn, _graph, _body):
        return 0

    runtime._count_cypher = count_cypher
    out = await runtime._astats()

    assert out.get("manifold") == {"turns": 7, "nouns": 5, "mentions": 3}


def test_validate_bind_host_loopback_only():
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("localhost") == "127.0.0.1"
    with pytest.raises(ValueError):
        validate_bind_host("0.0.0.0")
    with pytest.raises(ValueError):
        validate_bind_host("192.168.1.10")


def test_routes_read_and_mutations():
    assert match_route("GET", "/api/librarian/graph/stats")[0] == "stats"
    assert match_route("GET", "/api/librarian/graph/3d")[0] == "graph_3d"
    assert match_route("GET", "/api/health")[0] == "health"
    assert match_route("GET", "/api/librarian/nodes/10133099161583617") == (
        "node",
        {"vid": "10133099161583617"},
    )
    assert match_route("GET", "/api/librarian/nodes/1/audit")[0] == "audit"
    assert match_route("DELETE", "/api/librarian/nodes/1")[0] == "not_implemented"
    assert match_route("PATCH", "/api/librarian/nodes/1")[0] == "not_implemented"
    assert match_route("POST", "/api/librarian/nodes/merge")[0] == "not_implemented"
    assert match_route("GET", "/nope")[0] == "not_found"


def test_pack_search_emits_paths_and_graph():
    n = {"id": "1", "name": "Postgres", "label": "Noun"}
    m = {"id": "2", "name": "AGE", "label": "Noun"}
    out = pack_search(
        "tokyo",
        4,
        2,
        [{"id": 9, "content": "tokyo eye", "similarity": 0.81, "src": "conversation"}],
        ["1"],
        [(n, "mentions", m, 1.0, 0.72, 1)],
    )
    assert out["paths"][0]["rel"] == "mentions"
    assert out["paths"][0]["from"] == "noun:1"
    assert out["paths"][0]["to"] == "noun:2"
    assert out["paths"][0]["seed"] is True
    assert out["paths"][0]["hop"] == 1
    assert out["graph"]["edges"][0]["weight"] == 1.0
    assert out["graph"]["edges"][0]["cosine"] == 0.72
    assert out["seeds"][0]["id"] == "conv:9"
    assert out["seeds"][0]["chunk_id"] == "conv_9"
    assert out["seeds"][0]["vertex_ids"] == ["noun:1"]
    assert out["retrieval"]["edges_traversed"] == 1


def test_pack_search_sparse_has_empty_paths():
    out = pack_search("x", 4, 2, [{"id": 3, "content": "none", "similarity": 0.2}], [], [])
    assert out["paths"] == []
    assert out["retrieval"]["edges_traversed"] == 0
    assert out["ranked"][0]["group"] == "memory"


def test_unpack_expand_row_legacy_6tuple_hop():
    n, rel, m, w, c, decay, score, hop = unpack_expand_row(("n", "ABOUT", "m", 1.0, 0.72, 2))
    assert hop == 2
    assert decay is None
    assert score is None
    assert w == 1.0 and c == 0.72


def test_unpack_expand_row_7tuple_decay_is_not_hop():
    decay = 0.4
    score = 0.5 * 0.72 + 0.3 * 1.0 + 0.2 * decay
    n, rel, m, w, c, d, s, hop = unpack_expand_row(("n", "ABOUT", "m", 1.0, 0.72, decay, score))
    assert hop == 1
    assert d == decay
    assert abs(s - score) < 1e-9


def test_unpack_expand_row_8tuple_hop_after_score():
    decay = 0.4
    score = 0.5 * 0.72 + 0.3 * 1.0 + 0.2 * decay
    _, _, _, _, _, d, s, hop = unpack_expand_row(("n", "ABOUT", "m", 1.0, 0.72, decay, score, 2))
    assert hop == 2
    assert d == decay
    assert abs(s - score) < 1e-9


def test_pack_search_7tuple_plus_hop_keeps_hop():
    n = {"id": "1", "name": "Postgres", "label": "Noun"}
    m = {"id": "2", "name": "AGE", "label": "Noun"}
    decay = 0.4
    score = 0.5 * 0.72 + 0.3 * 1.0 + 0.2 * decay
    out = pack_search(
        "tokyo",
        4,
        2,
        [{"id": 9, "content": "tokyo eye", "similarity": 0.81}],
        ["1"],
        [(n, "mentions", m, 1.0, 0.72, decay, score, 2)],
    )
    assert out["paths"][0]["hop"] == 2
    assert out["paths"][0]["decay"] == decay
    assert abs(out["paths"][0]["score"] - score) < 1e-9
    assert abs(out["ranked"][0]["rank_score"] - score) < 1e-9


def test_pack_search_accepts_noun_dicts_and_audit_ids():
    from hermes_memory.walk import WalkRow

    row = WalkRow(
        (
            {"id": "11", "name": "SourceNoun", "label": "Noun"},
            "mentions",
            {"id": "12", "name": "TargetNoun", "label": "Noun"},
            4.0,
            0.8,
            1.0,
            0.55,
        ),
        audit={
            "session_id": "session-a",
            "turn_id": 41,
            "chunk_id": "conv_41",
            "hop": 1,
        },
    )

    out = pack_search(
        "noun",
        4,
        2,
        [{"id": "41", "content": "noun seed", "similarity": 0.8}],
        ["11"],
        [row],
    )

    assert out["graph"]["nodes"] == [
        {"id": "noun:11", "label": "Noun", "name": "SourceNoun", "props": {}, "val": 2},
        {"id": "noun:12", "label": "Noun", "name": "TargetNoun", "props": {}, "val": 2},
    ]
    assert out["paths"][0]["session_id"] == "session-a"
    assert out["paths"][0]["turn_id"] == 41
    assert out["paths"][0]["chunk_id"] == "conv_41"
    assert out["paths"][0]["hop"] == 1


def test_pack_search_omits_legacy_concept_about_rows():
    noun = {"id": "1", "name": "Postgres", "label": "Noun"}
    other = {"id": "2", "name": "AGE", "label": "Noun"}
    turn = {"id": 1, "label": "Turn", "properties": {"content": "legacy"}}
    concept = {"id": 3, "label": "Concept", "properties": {"name": "Legacy"}}

    out = pack_search(
        "postgres",
        4,
        2,
        [],
        ["1"],
        [
            (noun, "mentions", other, 2.0, 0.8, 1.0, 0.6, 1),
            (turn, "ABOUT", concept, 1.0, 0.7, 1.0, 0.5, 1),
        ],
    )

    assert {node["id"] for node in out["graph"]["nodes"]} == {"noun:1", "noun:2"}
    assert {edge["label"] for edge in out["graph"]["edges"]} == {"mentions"}


@pytest.mark.asyncio
async def test_search_builds_hypotheses_only_from_conversation_passports():
    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.graph_api import Runtime
    from hermes_memory.walk import WalkRow

    class FakeEmbedder:
        async def embed_text(self, _text):
            return [1.0, 0.0]

    class FakeStore:
        def __init__(self):
            self.hypotheses = None
            self.calls = 0

        async def vector_search(self, _literal, _k):
            return [
                {
                    "id": "41",
                    "content": "conversation seed",
                    "similarity": 0.8,
                    "src": "conversation",
                    "embedding": "[1,0]",
                },
                {
                    "id": "doc:1",
                    "content": "file seed",
                    "similarity": 0.9,
                    "src": "doc_chunk",
                },
            ]

        async def passports_for_conversations(self, conv_ids):
            assert conv_ids == [41]
            return [
                {
                    "noun_id": 11,
                    "chunk_id": "conv_41",
                    "session_id": "session-a",
                    "turn_id": 41,
                },
                {
                    "noun_id": 12,
                    "chunk_id": "conv_41",
                    "session_id": "session-a",
                    "turn_id": 41,
                },
            ]

        async def expand_graph(self, hypotheses, *, q_vec, hops, k):
            self.calls += 1
            self.hypotheses = hypotheses
            assert q_vec == [1.0, 0.0]
            assert hops == 2
            assert k == 4
            return [
                WalkRow(
                    (
                        {"id": "11", "name": "SourceNoun", "label": "Noun"},
                        "mentions",
                        {"id": "13", "name": "TargetNoun", "label": "Noun"},
                        4.0,
                        1.0,
                        1.0,
                        0.7,
                    ),
                    audit={
                        "session_id": "session-a",
                        "turn_id": 41,
                        "chunk_id": "conv_41",
                        "hop": 1,
                    },
                )
            ]

    runtime = Runtime.__new__(Runtime)
    runtime.store = FakeStore()
    runtime.embedder = FakeEmbedder()
    runtime.cfg = HybridAgeConfig(embed_dim=2)

    out = await runtime._asearch("noun", 4, 2)

    assert runtime.store.calls == 1
    assert len(runtime.store.hypotheses) == 2
    assert {h.noun_id for h in runtime.store.hypotheses} == {11, 12}
    assert all(h.chunk_id == "conv_41" for h in runtime.store.hypotheses)
    assert all(h.chunk_vec == [1.0, 0.0] for h in runtime.store.hypotheses)
    assert {result["content"] for result in out["results"]} == {
        "conversation seed",
        "file seed",
    }
    assert out["paths"][0]["session_id"] == "session-a"


def test_undirected_knn_emits_pair_selected_by_one_side_only():
    """Higher-id vertex selecting a lower-id neighbor must still emit the edge."""
    edges = undirected_knn_edges({
        "9": [(0.91, "3")],  # 9's top-1 is 3
        "3": [(0.88, "1")],  # 3's top-1 is someone else
        "1": [(0.88, "3")],
    })
    pairs = {(a, b) for a, b, _ in edges}
    assert ("3", "9") in pairs
    assert ("1", "3") in pairs
