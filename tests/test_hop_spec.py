"""test_hop_spec — gate the String-ID-for-bigint invariant the swarm surfaced.

Peer review 2026-09-08 flagged AGE vertex ids + conversations.id BIGSERIAL
exceeding JS Number.MAX_SAFE_INTEGER (9007199254740991) if serialized as bare
numbers. The live graph_view already does this correctly via stringify_id →
parse_vertex → pack_search (verified by curl on 5450/7890 live):

  curl /api/librarian/graph/3d  → nodes 1828, ids "noun:1","noun:2" str
  curl /api/librarian/nouns/1/hop → {"id":"noun:1" str}

This suite locks that invariant in CI without needing a live DB. A future
regression that returns a Number must fail here before it can ship.

Spec: docs/PANE_INTERACTION_SPEC.md §§1-3 (hover identity String-ID);
      docs/CORRECTIONS.md #14; swarm-2026-09-08-B/pane-spec-conversational.md
"""

from hermes_memory.graph_view import (
    parse_vertex,
    pack_search,
    stringify_id,
    parse_vertex_id_param,
    humanize_node,
)


def test_stringify_id_bigint_beyond_MAX_SAFE():
    # 9007199254740993 = 2**53 + 1, the canonical JS truncation case
    raw = 9007199254740993
    s = stringify_id(raw)
    assert isinstance(s, str)
    assert s == "9007199254740993"
    # Round-trip via int() must preserve exact value
    assert int(s) == raw
    # And must not equal the truncated Number value JS would produce
    assert int(s) != 9007199254740992


def test_stringify_id_also_handles_string_input():
    assert stringify_id("9007199254740993") == "9007199254740993"
    assert isinstance(stringify_id("9007199254740993"), str)
    # quoted string stripped
    assert stringify_id('"123"') == "123"


def test_parse_vertex_id_is_string():
    # Minimal AGE vertex JSON shape as returned by cypher
    raw_vertex = {"id": 9007199254740993, "label": "Noun", "properties": {"label": "test"}}
    parsed = parse_vertex(raw_vertex)
    assert parsed is not None
    # parse_vertex must have stringified via stringify_id
    assert isinstance(parsed["id"], str)
    assert parsed["id"] == "9007199254740993"


def test_parse_vertex_id_string_input_also_string():
    raw_vertex = {"id": "123", "label": "Noun", "properties": {"label": "x"}}
    parsed = parse_vertex(raw_vertex)
    assert isinstance(parsed["id"], str)


def test_pack_search_ids_are_prefixed_strings():
    # Build two minimal vertices and a triple, then ensure pack_search prefixes with "noun:"
    v1 = {"id": 1, "label": "Noun", "properties": {"label": "a"}}
    v2 = {"id": 2, "label": "Noun", "properties": {"label": "b"}}
    triple = (v1, "mentions", v2, 0.75, 0.5)
    packed = pack_search(
        q="test",
        k=8,
        hops=1,
        seeds=[],
        seed_vertex_ids=[],
        triples=[triple],
    )
    nodes = packed["graph"]["nodes"]
    edges = packed["graph"]["edges"]
    assert len(nodes) == 2
    assert len(edges) == 1
    for n in nodes:
        assert isinstance(n["id"], str), f"node id must be str, got {type(n['id'])}"
        assert n["id"].startswith("noun:"), f"expected noun: prefix, got {n['id']}"
    for e in edges:
        assert isinstance(e["source"], str)
        assert isinstance(e["target"], str)
        assert isinstance(e["from"], str)
        assert isinstance(e["to"], str)


def test_hop_id_param_round_trips_bigint():
    # Path param comes in as string, must survive as string before int() for DB lookup
    raw = "9007199254740993"
    s = stringify_id(raw)
    assert s == raw
    assert isinstance(s, str)
    assert parse_vertex_id_param(raw) == 9007199254740993


def test_humanize_node_preserves_string_id():
    v = {"id": 9007199254740993, "label": "Noun", "properties": {"label": "x"}}
    parsed = parse_vertex(v)
    human = humanize_node(parsed)
    assert isinstance(human["id"], str)
    assert human["id"] == "9007199254740993"
