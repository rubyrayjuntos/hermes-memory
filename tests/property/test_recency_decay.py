"""P10 — Recency decay ordering (issue #38).

- Decay fallback 0.5 for legacy (missing created_at).
- Composite score 0.5*cosine + 0.3*weight + 0.2*exp(-age/30) orders newer > older when w×c tied.
- recency_decay is monotonic decreasing with age.
"""
from __future__ import annotations

import datetime
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from hermes_memory.store import recency_decay, recency_decay_for_edge, recency_score


def test_recency_decay_fallback_none():
    assert recency_decay(None) == 0.5
    assert recency_decay("") == 0.5
    assert recency_decay("null") == 0.5
    assert recency_decay("not-a-date") == 0.5


def test_recency_decay_for_edge_prefers_edge_then_vertex():
    now = datetime.datetime(2026, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    edge_iso = "2026-01-15T12:00:00+00:00"
    vertex_iso = "2025-01-01T00:00:00+00:00"
    # edge wins when present
    d = recency_decay_for_edge(edge_iso, vertex_iso, now=now)
    assert abs(d - 1.0) < 1e-9
    # edge missing -> vertex
    age_days = (now - datetime.datetime.fromisoformat(vertex_iso)).total_seconds() / 86400
    expected = math.exp(-age_days / 30)
    assert abs(recency_decay_for_edge(None, vertex_iso, now=now) - expected) < 1e-9
    assert abs(recency_decay_for_edge("", vertex_iso, now=now) - expected) < 1e-9
    # both missing -> 0.5
    assert recency_decay_for_edge(None, None, now=now) == 0.5
    assert recency_decay_for_edge(None, "null", now=now) == 0.5


def test_recency_decay_monotonic():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    recent = now.isoformat()
    older = (now - datetime.timedelta(days=30)).isoformat()
    oldest = (now - datetime.timedelta(days=90)).isoformat()
    assert recency_decay(recent, now=now) > recency_decay(older, now=now) > recency_decay(oldest, now=now)
    assert recency_decay(oldest, now=now) > 0
    assert recency_decay(recent, now=now) <= 1.0


def test_recency_decay_future_clamps():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    future = (now + datetime.timedelta(days=10)).isoformat()
    assert abs(recency_decay(future, now=now) - 1.0) < 1e-9


def test_recency_score_formula():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    recent = now.isoformat()
    w, c = 0.8, 0.9
    decay = recency_decay(recent, now=now)  # ~1.0
    expected = 0.5 * c + 0.3 * w + 0.2 * decay
    assert abs(recency_score(w, c, created_at=recent, now=now) - expected) < 1e-9


def test_recency_score_fallback_decay():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    # legacy None -> decay 0.5
    assert abs(recency_score(0.8, 0.9, created_at=None, now=now) - (0.5 * 0.9 + 0.3 * 0.8 + 0.2 * 0.5)) < 1e-9


@settings(max_examples=200)
@given(
    w=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    c=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    age_old=st.integers(min_value=30, max_value=365),
    age_new=st.integers(min_value=0, max_value=10),
)
def test_property_recency_new_wins_same_wc(w, c, age_old, age_new):
    """Property: same weight×cosine, newer edge must rank higher."""
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    old_iso = (now - datetime.timedelta(days=age_old)).isoformat()
    new_iso = (now - datetime.timedelta(days=age_new)).isoformat()
    s_old = recency_score(w, c, created_at=old_iso, now=now)
    s_new = recency_score(w, c, created_at=new_iso, now=now)
    # newer should strictly win (exp is strictly decreasing); allow tiny epsilon for age_new==age_old boundary but our ranges ensure old>new
    assert s_new > s_old, f"new {new_iso} score {s_new} should beat old {old_iso} score {s_old} (w={w},c={c})"

@settings(max_examples=150)
@given(
    w=st.floats(min_value=0.1, max_value=1.0, allow_nan=False, allow_infinity=False),
    c=st.floats(min_value=0.1, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_property_decay_contributes(w, c):
    """Composite must equal weighted sum; decay term bounded [0.5 floor only when missing, but real decay in (0,1])."""
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    recent = now.isoformat()
    old = (now - datetime.timedelta(days=60)).isoformat()
    s_recent = recency_score(w, c, created_at=recent, now=now)
    s_old = recency_score(w, c, created_at=old, now=now)
    s_legacy = recency_score(w, c, created_at=None, now=now)
    # recent > legacy > old for sufficiently old? legacy decay 0.5, old exp(-60/30)=0.135 <0.5, so legacy > old
    assert s_recent > s_legacy > s_old
