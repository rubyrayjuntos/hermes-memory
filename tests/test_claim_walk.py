from hermes_memory.claim_walk import walk


def test_two_hops_follow_the_chain_and_do_not_loop():
    edges = {
        "a": [("claim_ab", "b")],
        "b": [("claim_bc", "c")],
        "c": [("claim_ca", "a")],
    }
    found = walk(["a"], edges, hops=2, per_node=8, per_hop=20)
    assert "claim_ab" in found
    assert "claim_bc" in found
    assert "claim_ca" not in found
