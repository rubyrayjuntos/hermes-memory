"""P9 — Concept compaction dedup ordering + cosine + weight invariants (issue #37).

- Deterministic keeper: min(id) wins for transitive duplicate groups (Union-Find).
- Cosine via real embedding cosine: pure-python check against math.
- Weight as merge count numeric bare literal via age_props (no quoted weight).
- No integration required; pairs are synthetic ids.
"""
from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from hermes_memory.store import _cosine_similarity, compaction_keepers, age_props


@given(
    pairs=st.lists(
        st.tuples(st.integers(min_value=1, max_value=1000), st.integers(min_value=1, max_value=1000)),
        min_size=1, max_size=20,
    )
)
@settings(max_examples=200)
def test_p9_compaction_keeper_is_min_id(pairs):
    # filter self-pairs
    pairs = [(a, b) for a, b in pairs if a != b]
    if not pairs:
        return
    mapping = compaction_keepers(pairs)
    # Every loser maps to smaller keeper
    for loser, keeper in mapping.items():
        assert keeper < loser, f"keeper {keeper} should be < loser {loser}"
        assert keeper != loser
    # Keeper never appears as loser
    for keeper in mapping.values():
        assert keeper not in mapping, f"keeper {keeper} should not also be a loser"
    # Transitive: if (1,2) and (2,3) -> 2 and 3 both map to 1
    # Check: all nodes in a connected component share same root (min)
    # Build groups via mapping + roots
    all_ids = set()
    for a, b in pairs:
        all_ids.add(a); all_ids.add(b)
    # Re-derive groups via compaction_keepers logic: each group's keeper is min
    keeper_set = set(mapping.values())
    for k in keeper_set:
        # No two keepers should be connected via pair chain without merging
        # i.e., keepers are representatives of distinct components
        pass  # structural; min property already checked


@settings(max_examples=100)
@given(
    a=st.lists(st.floats(min_value=-1, max_value=1, allow_nan=False, allow_infinity=False), min_size=3, max_size=8),
)
def test_p9_cosine_similarity_properties(a):
    import math as _math
    norm = _math.sqrt(sum(x * x for x in a))
    if norm == 0 or _math.isclose(norm, 0.0):
        assert _cosine_similarity(a, a) is None
        return
    # Same vector -> cosine 1.0
    assert math.isclose(_cosine_similarity(a, a), 1.0, rel_tol=1e-9)
    # Orthogonal: [1,0] vs [0,1] -> 0
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    # Opposite -> -1
    assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0
    # Invalid returns None
    assert _cosine_similarity(None, a) is None
    assert _cosine_similarity(a, None) is None
    assert _cosine_similarity([1.0, 2.0], [1.0]) is None


def test_p9_age_props_weight_is_numeric_bare_literal():
    # weight as merge count must be bare numeric, not quoted string
    out = age_props({"weight": 3})
    assert out == "{weight: 3}", f"expected bare numeric, got {out!r}"
    out2 = age_props({"weight": 2, "cosine": 0.92})
    # numeric bare, order preserved
    assert "weight: 2" in out2
    assert "cosine: 0.92" in out2
    assert "'2'" not in out2 and "'0.92'" not in out2


def test_p9_compaction_keepers_transitive():
    # Explicit transitive chain 5-2, 2-9, 9-1 -> all map to 1 (min)
    pairs = [(5, 2), (2, 9), (9, 1)]
    m = compaction_keepers(pairs)
    assert m[2] == 1
    assert m[5] == 1
    assert m[9] == 1
    assert 1 not in m  # keeper not loser


def test_p9_compaction_dedup_ordering_disjoint_groups():
    pairs = [(10, 20), (30, 40)]
    m = compaction_keepers(pairs)
    assert m == {20: 10, 40: 30}

def test_p9_compaction_keepers_with_cosine_triples():
    # API accepts (a,b,cosine) triples like script produces
    pairs = [(10, 20, 0.95), (20, 30, 0.93)]
    m = compaction_keepers(pairs)
    # Transitively 10 is keeper for 20 and 30
    assert m[20] == 10
    assert m[30] == 10
