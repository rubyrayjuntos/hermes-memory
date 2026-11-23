"""Spec 7 T7.1: tests first, per the spec's own 7.2 test list.

``find_segment_boundaries`` is pure (no DB, no I/O) -- same convention as
``work_status.classify_verdict`` and ``extraction_worker._is_synthetic_user_row``.
Each test number below matches docs/hermes-memory-dev-spec.md section 7.2.
"""
from __future__ import annotations

from hermes_memory.turn_boundaries import SpanRange, find_segment_boundaries


def _span(id_, role, content=""):
    return {"id": id_, "role": role, "content": content}


def test_1_single_segment_no_false_split_from_tool_call_volume():
    spans = [
        _span(1, "user", "do the thing"),
        _span(2, "assistant", "ok, working on it"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 2, closed=False)]


def test_2_second_real_ask_after_assistant_exchange_opens_new_segment():
    spans = [
        _span(1, "user", "do the first thing"),
        _span(2, "assistant", "done with the first thing"),
        _span(3, "user", "now do a second, unrelated thing"),
        _span(4, "assistant", "done with the second thing"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [
        SpanRange(1, 2, closed=True),
        SpanRange(3, 4, closed=False),
    ]


def test_3_no_exemption_list_every_qualifying_user_row_is_a_boundary():
    # 2026-11-23 review: the spec's drafted continuation-cue exemption list
    # had 0/5 real matches in the corpus and was cut. This row LOOKS like a
    # continuation ("Also, ...") but still opens its own segment -- that is
    # the deliberate, reviewed behavior, not a gap.
    spans = [
        _span(1, "user", "check file A"),
        _span(2, "assistant", "checked file A, it's fine"),
        _span(3, "user", "Also, can you also check the other file?"),
        _span(4, "assistant", "checked file B too"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [
        SpanRange(1, 2, closed=True),
        SpanRange(3, 4, closed=False),
    ]


def test_4_synthetic_marker_is_always_a_hard_boundary():
    spans = [
        _span(1, "user", "start a long task"),
        _span(2, "assistant", "working on it"),
        _span(3, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] summary of prior work..."),
        _span(4, "assistant", "continuing"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [
        SpanRange(1, 2, closed=True),
        SpanRange(3, 4, closed=False),
    ]


def test_4b_synthetic_marker_is_a_hard_boundary_even_with_no_assistant_row_between():
    # A marker immediately after another marker (no assistant/tool activity
    # between them) is still a hard boundary -- hard boundaries don't need
    # signal 2's "work happened" gate.
    spans = [
        _span(1, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] first marker"),
        _span(2, "user", "[OUT-OF-BAND USER MESSAGE] second marker, no work between"),
        _span(3, "assistant", "ok"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [
        SpanRange(1, 1, closed=True),
        SpanRange(2, 3, closed=False),
    ]


def test_5_two_user_rows_with_no_assistant_between_stay_in_one_open_segment():
    spans = [
        _span(1, "user", "first part of the ask"),
        _span(2, "user", "second part, still the same ask, no work happened yet"),
        _span(3, "assistant", "ok, done"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 3, closed=False)]


def test_6_file_touch_merge_blocked_on_t5_5b_not_implemented_yet():
    # Signal 3 (touched-file/repo-set divergence) is explicitly "skipped
    # entirely" per 7.1 until T5.5b (work_event.refs) lands. find_segment_
    # boundaries() therefore takes no refs/work_event argument at all yet --
    # this test pins that signals 1+2 alone are the whole function today.
    import inspect

    sig = inspect.signature(find_segment_boundaries)
    assert list(sig.parameters) == ["spans"]


def test_7_full_span_range_is_exactly_partitioned_no_gaps_no_overlaps():
    spans = [
        _span(1, "user", "ask 1"),
        _span(2, "assistant", "reply 1"),
        _span(3, "user", "ask 2"),
        _span(4, "assistant", "reply 2"),
        _span(5, "user", "ask 3"),
        _span(6, "assistant", "reply 3"),
    ]
    result = find_segment_boundaries(spans)
    assert result[0].first_span_id == spans[0]["id"]
    assert result[-1].last_span_id == spans[-1]["id"]
    for prev, cur in zip(result, result[1:]):
        assert cur.first_span_id == prev.last_span_id + 1
    assert all(r.first_span_id <= r.last_span_id for r in result)


def test_empty_spans_returns_empty():
    assert find_segment_boundaries([]) == []


def test_single_span_returns_one_open_segment():
    result = find_segment_boundaries([_span(1, "user", "hello")])
    assert result == [SpanRange(1, 1, closed=False)]
