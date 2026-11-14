from hermes_memory.fusion import mmr, rrf


def test_rrf_promotes_an_id_that_appears_in_two_lists():
    fused = rrf([["a", "b", "c"], ["b", "d"]], k=60)
    assert fused[0] == "b"


def test_mmr_keeps_the_first_and_skips_a_near_duplicate():
    vectors = {"a": [1.0, 0.0], "a2": [0.99, 0.01], "c": [0.0, 1.0]}
    picked = mmr(["a", "a2", "c"], vectors, lam=0.7, limit=2)
    assert picked[0] == "a"
    assert "c" in picked
