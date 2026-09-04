"""Garden edge visibility and ship-copy contract."""
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st


def edge_visible(label: str, allow_imports: bool = False) -> bool:
    """Garden shows the flower and noun manifold; code imports are opt-in."""
    if label == "IMPORTS":
        return bool(allow_imports)
    return label in {"NEXT", "IN_SESSION", "mentions"}


def filter_edges(labels: list[str], allow_imports: bool = False) -> list[str]:
    return [lbl for lbl in labels if edge_visible(lbl, allow_imports)]


def test_mentions_visible_by_default_and_imports_off():
    assert edge_visible("mentions") is True
    assert edge_visible("IMPORTS") is False
    assert edge_visible("IMPORTS", allow_imports=True) is True


def test_fountain_is_garden_only_ship_surface():
    html = Path("docs/graph/fountain.html").read_text()

    assert "co-occurrence (mention order)" in html
    assert "Session" in html and "Turn" in html and "Noun" in html
    assert "ABOUT" not in html
    assert "Concept" not in html
    assert "level: 1" in html
    assert "graph/3d?limit=80" in html


def test_readme_advertises_garden_manifold_only():
    readme = Path("README.md").read_text()

    assert "ABOUT" not in readme
    assert "Concept" not in readme
    assert "3d.html" not in readme
    assert "0.4" in readme and "magnitude / 8" in readme
    assert "http://127.0.0.1:7890/api/librarian/pane" in readme


@settings(max_examples=100)
@given(
    labels=st.lists(
        st.sampled_from(["IMPORTS", "NEXT", "IN_SESSION", "mentions"]),
        min_size=0,
        max_size=20,
    ),
    allow_imports=st.booleans(),
)
def test_property_edge_filter_respects_imports_default(labels, allow_imports):
    filtered = filter_edges(labels, allow_imports)
    if not allow_imports:
        assert "IMPORTS" not in filtered
    assert all(label in {"IMPORTS", "NEXT", "IN_SESSION", "mentions"} for label in filtered)
    assert set(filtered).issubset(set(labels))
    expected_len = sum(1 for lbl in labels if edge_visible(lbl, allow_imports))
    assert len(filtered) == expected_len
