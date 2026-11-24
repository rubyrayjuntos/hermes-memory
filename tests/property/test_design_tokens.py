"""Design-system invariants for docs/design-system (The Hermes Librarian).

tokens.json is the single source of truth; these tests pin the properties the
brand book promises so a token edit cannot silently break them:

* tokens.css is exactly what scripts/build_design_tokens.py compiles;
* brand colours match the style guide PDF;
* every text/mark pair meets its WCAG contrast floor in EVERY theme;
* every var(--x) the component stylesheet uses is declared;
* the compiler resolves any acyclic alias graph and rejects any cycle.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
DS = ROOT / "docs" / "design-system"

_spec = importlib.util.spec_from_file_location(
    "build_design_tokens", ROOT / "scripts" / "build_design_tokens.py"
)
assert _spec and _spec.loader
bdt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdt)

TOKENS = json.loads((DS / "tokens.json").read_text(encoding="utf-8"))
THEMES = [t["id"] for t in TOKENS["color"]["themes"]]
TABLE = {t["name"]: bdt._themed(t["value"], THEMES) for t in TOKENS["color"]["tokens"]}


def color(name: str, theme: str) -> str:
    return bdt.resolve_color(name, theme, TABLE, THEMES[0])


def luminance(hex_: str) -> float:
    h = hex_.lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------------------------------- sync


def test_tokens_css_is_compiled_from_tokens_json() -> None:
    assert (DS / "tokens.css").read_text(encoding="utf-8") == bdt.compile_css(TOKENS), (
        "docs/design-system/tokens.css is stale: run python scripts/build_design_tokens.py"
    )


def test_brand_palette_matches_style_guide() -> None:
    guide = {
        "brand-nous-gold": "#dd8e35",
        "brand-librarian-gold": "#c9a86a",
        "brand-lapis": "#1a3a5f",
        "brand-parchment": "#f7f3eb",
        "brand-ink": "#0d1117",
        "brand-vector-dot": "#f5c518",
        "brand-graph-edge": "#5b8def",
        "brand-weed-red": "#8b3a2e",
    }
    for name, hex_ in guide.items():
        for th in THEMES:
            assert color(name, th).lower() == hex_, name


# ----------------------------------------------------------------------- contrast

SURFACES = ("surface", "surface-raised", "surface-sunken")
ENTITY_TYPES = ("person", "project", "place", "org", "tool", "concept", "other")
TEXT_4_5 = [("text", s) for s in SURFACES] + [("text-muted", s) for s in SURFACES] + [
    ("on-accent", "accent"),
    ("accent", "surface"),
    ("accent", "surface-raised"),
] + [(f"entity-{e}", "surface") for e in ENTITY_TYPES]
MARKS_3_0 = [
    (fg, s)
    for fg in (
        "line-strong",
        "focus-ring",
        "accent",
        "seal",
        "vector",
        "graph-edge",
        "weed",
        "status-grounded",
        "status-unconfirmed",
        "status-leased",
        "status-retracted",
        "status-orphaned",
        *(f"entity-{e}" for e in ENTITY_TYPES),
    )
    for s in SURFACES
]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize(("fg", "bg"), TEXT_4_5)
def test_text_contrast_at_least_4_5(theme: str, fg: str, bg: str) -> None:
    ratio = contrast(color(fg, theme), color(bg, theme))
    assert ratio >= 4.5, f"{fg} on {bg} in {theme}: {ratio:.2f}:1"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize(("fg", "bg"), MARKS_3_0)
def test_mark_contrast_at_least_3(theme: str, fg: str, bg: str) -> None:
    ratio = contrast(color(fg, theme), color(bg, theme))
    assert ratio >= 3.0, f"{fg} on {bg} in {theme}: {ratio:.2f}:1"


@pytest.mark.parametrize("theme", THEMES)
def test_success_and_failure_differ_beyond_hue(theme: str) -> None:
    """Grounded/done must not be told from retracted/failed by red-green hue alone:
    success is blue-dominant (blue channel above both red and green)."""
    h = color("status-grounded", theme).lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    assert b > r and b > g, f"status-grounded drifted toward red/green in {theme}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("status-leased", "status-unconfirmed"),
        ("status-grounded", "status-leased"),
        ("status-retracted", "status-orphaned"),
    ],
)
def test_adjacent_statuses_differ_in_lightness(theme: str, a: str, b: str) -> None:
    """Statuses that sit side by side in the jobs/claims tables must differ in
    lightness (contrast between them >= 1.4), not only in hue."""
    ratio = contrast(color(a, theme), color(b, theme))
    assert ratio >= 1.4, f"{a} vs {b} in {theme}: {ratio:.2f}"


def _lab(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    lin = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        for c in (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    ]
    r, g, b = lin
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(v: float) -> float:
        return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


@pytest.mark.parametrize("theme", THEMES)
def test_entity_type_colors_are_mutually_distinct(theme: str) -> None:
    """Graph vertices are told apart by type colour: every pair >= 20 ΔE76 (CIELAB)."""
    labs = {e: _lab(color(f"entity-{e}", theme)) for e in ENTITY_TYPES}
    for i, a in enumerate(ENTITY_TYPES):
        for b in ENTITY_TYPES[i + 1 :]:
            d = sum((p - q) ** 2 for p, q in zip(labs[a], labs[b])) ** 0.5
            assert d >= 20, f"entity-{a} vs entity-{b} in {theme}: ΔE {d:.1f}"


# --------------------------------------------------------------- stylesheet wiring


def test_component_css_uses_only_declared_tokens() -> None:
    css = (DS / "components.css").read_text(encoding="utf-8")
    declared = set(re.findall(r"--([A-Za-z0-9_.-]+)\s*:", (DS / "tokens.css").read_text()))
    declared |= set(re.findall(r"--([A-Za-z0-9_.-]+)\s*:", css))  # local custom props
    used = set(re.findall(r"var\(--([A-Za-z0-9_.-]+)", css))
    assert used <= declared, f"undeclared: {sorted(used - declared)}"


def test_component_css_has_no_raw_colors() -> None:
    css = (DS / "components.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), "raw hex in components.css"


# ---------------------------------------------------------------- compiler props

_names = st.lists(
    st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True), min_size=1, max_size=12, unique=True
)
_hex = st.from_regex(r"#[0-9a-f]{6}", fullmatch=True)


@st.composite
def acyclic_token_sets(draw: st.DrawFn) -> dict:
    names = draw(_names)
    toks = []
    for i, n in enumerate(names):
        vals = {}
        for th in ("ink", "parchment"):
            if th == "parchment" and draw(st.booleans()):
                continue  # inherit from the first theme
            # alias only to an EARLIER name: acyclic by construction
            if i and draw(st.booleans()):
                vals[th] = "{" + names[draw(st.integers(0, i - 1))] + "}"
            else:
                vals[th] = draw(_hex)
        toks.append({"name": n, "value": vals, "usage": "x"})
    return {
        "name": "T",
        "color": {"themes": [{"id": "ink"}, {"id": "parchment"}], "tokens": toks},
        "type": {"families": {"sans": "x"}, "groups": []},
    }


@settings(max_examples=150, deadline=None)
@given(acyclic_token_sets())
def test_any_acyclic_alias_graph_compiles_and_resolves_to_hex(tokens: dict) -> None:
    css = bdt.compile_css(tokens)
    assert bdt.compile_css(tokens) == css  # deterministic
    themes = ["ink", "parchment"]
    table = {t["name"]: bdt._themed(t["value"], themes) for t in tokens["color"]["tokens"]}
    first_block = css.split("}")[0]
    for t in tokens["color"]["tokens"]:
        assert first_block.count(f"--{t['name']}:") == 1
        for th in themes:
            assert bdt.HEX_RE.match(bdt.resolve_color(t["name"], th, table, "ink"))


@settings(max_examples=100, deadline=None)
@given(acyclic_token_sets(), st.data())
def test_any_cycle_is_rejected(tokens: dict, data: st.DataObject) -> None:
    toks = tokens["color"]["tokens"]
    i = data.draw(st.integers(0, len(toks) - 1))
    toks[i]["value"] = {"ink": "{" + toks[i]["name"] + "}"}  # self-cycle
    with pytest.raises(bdt.TokenError):
        bdt.compile_css(tokens)
