"""Unit tests for the viz API — no database required."""
from __future__ import annotations

import json

import pytest

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
    n = {"id": 1, "label": "Turn", "properties": {"name": "hello", "content": "hello tokyo"}}
    m = {"id": 2, "label": "Concept", "properties": {"name": "Tokyo Eye"}}
    out = pack_search(
        "tokyo",
        4,
        2,
        [{"id": 9, "content": "tokyo eye", "similarity": 0.81}],
        ["1"],
        [(n, "ABOUT", m, 1.0, 0.72, 1)],
    )
    assert out["paths"][0]["rel"] == "ABOUT"
    assert out["paths"][0]["from"] == "1"
    assert out["paths"][0]["to"] == "2"
    assert out["paths"][0]["seed"] is True
    assert out["paths"][0]["hop"] == 1
    assert out["graph"]["edges"][0]["weight"] == 1.0
    assert out["graph"]["edges"][0]["cosine"] == 0.72
    assert out["seeds"][0]["vertex_ids"] == ["1"]
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
    n = {"id": 1, "label": "Turn", "properties": {"name": "hello", "content": "hello tokyo"}}
    m = {"id": 2, "label": "Concept", "properties": {"name": "Tokyo Eye"}}
    decay = 0.4
    score = 0.5 * 0.72 + 0.3 * 1.0 + 0.2 * decay
    out = pack_search(
        "tokyo",
        4,
        2,
        [{"id": 9, "content": "tokyo eye", "similarity": 0.81}],
        ["1"],
        [(n, "ABOUT", m, 1.0, 0.72, decay, score, 2)],
    )
    assert out["paths"][0]["hop"] == 2
    assert out["paths"][0]["decay"] == decay
    assert abs(out["paths"][0]["score"] - score) < 1e-9


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
