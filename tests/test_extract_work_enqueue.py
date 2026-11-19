"""Spec 5 T5.2: on_session_end enqueues one extract_work job, idempotently,
gated behind work_capture."""
from __future__ import annotations

from hermes_memory.provider import _extract_work_op_id, _extract_work_payload


def test_extract_work_op_id_is_stable_for_the_same_range():
    assert _extract_work_op_id("s1", 10, 20) == _extract_work_op_id("s1", 10, 20)


def test_extract_work_op_id_differs_when_the_range_grows():
    """A session that gets new spans after a prior enqueue must produce a
    NEW op id -- idempotency is per-range, not a permanent one-shot-ever
    lock on the session_id."""
    first = _extract_work_op_id("s1", 10, 20)
    grown = _extract_work_op_id("s1", 10, 25)
    assert first != grown


def test_extract_work_op_id_differs_across_sessions():
    assert _extract_work_op_id("s1", 10, 20) != _extract_work_op_id("s2", 10, 20)


def test_extract_work_payload_is_ids_only_no_text():
    payload = _extract_work_payload("owner-a", "s1", 10, 20)
    assert payload == {
        "owner": "owner-a", "session_id": "s1", "first_span_id": 10, "last_span_id": 20,
    }
    for value in payload.values():
        assert not isinstance(value, str) or len(value) < 64, (
            "payload must stay id-only -- a long string value would mean raw text leaked in"
        )
