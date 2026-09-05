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

from hermes_memory.store import recency_decay, recency_decay_for_edge


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


@settings(max_examples=200)
@given(
    age_old=st.integers(min_value=30, max_value=365),
    age_new=st.integers(min_value=0, max_value=10),
)
def test_property_recency_decay_newer_is_larger(age_old, age_new):
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    old_iso = (now - datetime.timedelta(days=age_old)).isoformat()
    new_iso = (now - datetime.timedelta(days=age_new)).isoformat()
    assert recency_decay(new_iso, now=now) > recency_decay(old_iso, now=now)


@settings(max_examples=150)
@given(age_days=st.integers(min_value=0, max_value=365))
def test_property_recency_decay_bounded(age_days):
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    iso = (now - datetime.timedelta(days=age_days)).isoformat()
    d = recency_decay(iso, now=now)
    assert 0.0 <= d <= 1.0
