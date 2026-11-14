"""Unit-level shape checks for explorer serializers (no live DB)."""
from __future__ import annotations

from datetime import datetime, timezone

from hermes_memory.store_explorer import _SCHEMA, _date, _excerpt, _iso, _lease_left


def test_iso_and_date():
    dt = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)
    assert _iso(dt) == "2026-03-02T15:00:00Z"
    assert _date(dt) == "2026-03-02"
    assert _iso(None) is None
    assert _date(None) == ""


def test_lease_left_expired_and_open():
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert _lease_left(past) == "expired"
    assert _lease_left(None) == "—"


def test_excerpt_truncates():
    assert _excerpt("short") == "short"
    long = "word " * 100
    out = _excerpt(long, limit=40)
    assert len(out) <= 40
    assert out.endswith("…")


def test_schema_catalog_covers_claim_layer():
    names = {row["table"] for row in _SCHEMA}
    assert {"entity", "entity_alias", "turn_entity", "claims", "claim_evidence",
            "durable_job", "conversations"}.issubset(names)
