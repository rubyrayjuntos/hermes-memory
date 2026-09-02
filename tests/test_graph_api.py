"""Unit tests for the viz API — no database required."""
from __future__ import annotations

import json

import pytest

from hermes_memory.graph_api import (
    clamp_limit,
    conversation_first_budget,
    match_route,
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
