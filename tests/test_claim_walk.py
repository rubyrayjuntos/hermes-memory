from hermes_memory.claim_walk import walk


def test_walk_preserves_claims_on_edges_to_visited_entities():
    edges = {
        "a": [("claim_ab", "b")],
        "b": [("claim_ba", "a")],
    }
    found = walk(["a"], edges, hops=2, per_node=8, per_hop=20)
    assert "claim_ab" in found
    assert "claim_ba" in found
