"""Unit tests for the viz API — no database required."""
from __future__ import annotations

import json

import pytest

from hermes_memory.graph_api import (
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
    validate_bind_host,
)


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
