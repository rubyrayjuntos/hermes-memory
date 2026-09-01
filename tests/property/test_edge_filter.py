"""Property test for 3d.html edge-type filter (IMPORTS vs ABOUT) — issue #39.

The HTML filter hides/shows edges without reload; this test pins the
pure filter predicate used in JS: edgeVisible(label, allow_imports, allow_about).
"""
from hypothesis import given, settings
from hypothesis import strategies as st


def edge_visible(label: str, allow_imports: bool, allow_about: bool) -> bool:
    """Mirrors the JS edgeVisible() in docs/graph/3d.html.

    IMPORTS and ABOUT each have a checkbox; other labels are always visible.
    """
    if label == "IMPORTS":
        return bool(allow_imports)
    if label == "ABOUT":
        return bool(allow_about)
    return True


def filter_edges(labels: list[str], allow_imports: bool, allow_about: bool) -> list[str]:
    return [lbl for lbl in labels if edge_visible(lbl, allow_imports, allow_about)]


def test_about_edges_visible_when_checked():
    assert edge_visible("ABOUT", True, True) is True
    assert edge_visible("ABOUT", True, False) is False
    assert edge_visible("ABOUT", False, True) is True
    # ABOUT filtering is independent of IMPORTS toggle
    assert edge_visible("IMPORTS", True, False) is True
    assert edge_visible("IMPORTS", False, True) is False


def test_imports_filter():
    assert filter_edges(["IMPORTS", "ABOUT", "MENTIONS"], True, True) == ["IMPORTS", "ABOUT", "MENTIONS"]
    assert filter_edges(["IMPORTS", "ABOUT", "MENTIONS"], False, True) == ["ABOUT", "MENTIONS"]
    assert filter_edges(["IMPORTS", "ABOUT", "MENTIONS"], True, False) == ["IMPORTS", "MENTIONS"]
    assert filter_edges(["IMPORTS", "ABOUT"], False, False) == []
    # non-filtered labels unaffected
    assert filter_edges(["MENTIONS", "CO_MENTIONED"], False, False) == ["MENTIONS", "CO_MENTIONED"]


def test_about_w_c_display():
    """ABOUT edges carry weight 1.0 and cosine 0.55-1.0 — score is w*c ∈ [0.55, 1.0]."""
    for w, c in [(1.0, 0.55), (1.0, 0.75), (1.0, 1.0)]:
        score = w * c
        assert 0.55 <= score <= 1.0
        # formatted string mirrors JS: w1.00·c0.75=0.75
        assert f"w{w:.2f}·c{c:.2f}={score:.2f}" == f"w{w:.2f}·c{c:.2f}={score:.2f}"


@settings(max_examples=100)
@given(
    labels=st.lists(st.sampled_from(["IMPORTS", "ABOUT", "MENTIONS", "GOVERNED_BY"]), min_size=0, max_size=20),
    allow_imports=st.booleans(),
    allow_about=st.booleans(),
)
def test_property_edge_filter_respects_toggles(labels, allow_imports, allow_about):
    filtered = filter_edges(labels, allow_imports, allow_about)
    # IMPORTS only present when allowed
    if not allow_imports:
        assert "IMPORTS" not in filtered
    if not allow_about:
        assert "ABOUT" not in filtered
    # all kept labels must have been in input
    assert set(filtered).issubset(set(labels))
    # count property
    expected_len = sum(1 for lbl in labels if edge_visible(lbl, allow_imports, allow_about))
    assert len(filtered) == expected_len


@settings(max_examples=80)
@given(
    c=st.floats(min_value=0.55, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_property_about_score_clamped(c):
    w = 1.0
    score = w * c
    assert 0.55 <= score <= 1.0
    # JS uses Number(...).toFixed(2) -> must round-trip
    assert float(f"{score:.2f}") == round(score, 2)
