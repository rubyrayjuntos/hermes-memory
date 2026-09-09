"""Unit tests for name resolution: transitive walk, survivor election, no forging."""
from datetime import datetime, timedelta, timezone

from hermes_memory.store import Store

R = Store._resolve


def _meta(rows):
    """rows: [(surface, canon, day_offset, alias_id)] -> canon-birth metadata.

    Mirrors Store.alias_graph: only canon birth ages a slug. A surface-only
    appearance (nickname row) never makes a slug older.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    m = {}
    for s, c, d, aid in rows:
        key = (base + timedelta(days=d), aid)
        if c not in m or key < m[c]:
            m[c] = key
    return m


def test_unresolved_falls_back_to_own_normalization():
    assert R("Tokyo Eye", {}) == ("tokyo eye", False)
    assert R("tokyo-eye", {}) == ("tokyo eye", False)
    # Distinct surfaces stay distinct — the fallback never forges equality.
    assert R("Tokyo Eye", {})[0] != R("Nightingale", {})[0]


def test_direct_and_transitive():
    assert R("TE", {"te": "tokyo eye"}) == ("tokyo eye", False)
    assert R("a", {"a": "b", "b": "c"}) == ("c", False)


def test_cycle_without_meta_falls_back_to_walk_order_not_lex():
    # Lex-smallest would answer "bb" both times; walk order answers the
    # entry slug. Either way, lexicography is never consulted.
    assert R("zz", {"zz": "bb", "bb": "zz"}) == ("zz", True)
    assert R("bb", {"zz": "bb", "bb": "zz"}) == ("bb", True)


def test_survivor_prefers_oldest_canon_birth_not_lex():
    amap = {"quad": "zzproject", "zzproject": "quad"}
    meta = _meta([
        ("quad", "zzproject", 1, 1),
        ("zzproject", "quad", 9, 2),
    ])
    # Lex would elect "quad" (q < z); oldest canon birth (zzproject, day 1)
    # wins. "quad" was only ever a surface until day 9 — surfaces don't age.
    assert R("quad", amap, meta) == ("zzproject", True)


def test_short_slug_loses_to_longer_live_alias():
    amap = {"te": "tokyo eye", "tokyo eye": "te"}
    meta = _meta([("te", "tokyo eye", 1, 1), ("tokyo eye", "te", 5, 2)])
    # "te" is the older equation here but under 4 chars with a longer
    # live alias in the component — the nickname must not win.
    assert R("te", amap, meta) == ("tokyo eye", True)


def test_self_loop_is_stable_not_cycle():
    assert R("q", {"q": "q"}) == ("q", False)
