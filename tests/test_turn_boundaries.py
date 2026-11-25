"""Spec 7 T7.1: tests first, per the spec's own 7.2 test list.

``find_segment_boundaries`` is pure (no DB, no I/O) -- same convention as
``work_status.classify_verdict`` and ``extraction_worker._is_synthetic_user_row``.
Each test number below matches docs/hermes-memory-dev-spec.md section 7.2.
"""
from __future__ import annotations

from hermes_memory.turn_boundaries import (
    SpanRange,
    find_segment_boundaries,
    find_status_lookahead_spans,
)


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


def test_4b_marker_only_segment_merges_into_the_next_segment():
    # 2026-11-24 fix: a marker should not stand alone as its own segment --
    # a degenerate marker-only segment (first==last==just the marker) merges
    # forward into whatever segment follows it. Real finding that drove
    # this: 450/3327 items (13.5%) in the first G7 eval run were nothing but
    # a marker, with one session alone producing 331 items from heavy
    # compaction activity.
    spans = [
        _span(1, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] first marker"),
        _span(2, "user", "[OUT-OF-BAND USER MESSAGE] second marker, no work between"),
        _span(3, "assistant", "ok"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 3, closed=False)]


def test_4c_marker_only_trailing_segment_merges_into_the_previous_segment():
    # The marker-only segment is last (no "next" to merge forward into) --
    # it must merge backward into the segment before it instead.
    spans = [
        _span(1, "user", "do the thing"),
        _span(2, "assistant", "done"),
        _span(3, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] trailing marker, nothing after"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 3, closed=False)]


def test_4d_session_that_is_only_a_marker_has_nothing_to_merge_with():
    # No neighbor exists at all -- the marker-only segment has to stay as
    # itself (this is the legitimate "marker-only session" case the
    # ask-selection fix's own fallback-to-marker test already covers).
    spans = [_span(1, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] the whole session is this")]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 1, closed=False)]


def test_4e_a_chain_of_consecutive_markers_all_merge_into_the_following_real_segment():
    spans = [
        _span(1, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] marker one"),
        _span(2, "user", "[OUT-OF-BAND USER MESSAGE] marker two"),
        _span(3, "user", "[ASYNC DELEGATION BATCH COMPLETE] marker three"),
        _span(4, "user", "a real ask, finally"),
        _span(5, "assistant", "ok, working on it"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 5, closed=False)]


def test_4f_compaction_marker_segment_merges_backward_into_the_prior_human_ask():
    # 2026-11-24 fix: a compaction marker should never become the ask --
    # unlike a background-process/delegation/cron marker (which really is
    # what started that unit of work), a compaction marker is pure
    # context-management scaffolding re-injecting a SUMMARY of earlier
    # human-driven work. Even when real assistant work follows it (this
    # segment is NOT marker-only -- it has 2 assistant turns), it must
    # fold backward into the segment holding the actual human ask that
    # came before it, not stand on its own.
    spans = [
        _span(1, "user", "please refactor the auth module"),
        _span(2, "assistant", "working on it"),
        _span(3, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] summary of prior work..."),
        _span(4, "assistant", "continuing the refactor"),
        _span(5, "assistant", "done, tests pass"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 5, closed=False)]


def test_4g_compaction_marker_as_the_very_first_segment_has_nothing_to_merge_into():
    # The whole session opens with a compaction marker (e.g. a session
    # that only ever picked up mid-task from a prior one) -- there is no
    # preceding human-ask segment to attach to, so it stays as its own
    # segment (same "nothing to merge with" fallback as 4d).
    spans = [
        _span(1, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] resuming prior work"),
        _span(2, "assistant", "continuing"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 2, closed=False)]


def test_4h_cron_background_and_delegation_markers_are_not_affected():
    # The user's explicit carve-out: these markers really ARE what
    # started the work (no human in the loop), so they keep opening
    # their own segment -- only compaction markers get the backward-merge
    # treatment.
    spans = [
        _span(1, "user", "please refactor the auth module"),
        _span(2, "assistant", "working on it"),
        _span(3, "user", "[IMPORTANT: Background process proc_abc123 completed normally]"),
        _span(4, "assistant", "noted, continuing"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [
        SpanRange(1, 2, closed=True),
        SpanRange(3, 4, closed=False),
    ]


def test_4_synthetic_marker_is_always_a_hard_boundary():
    # 2026-11-24: this now merges per the compaction-marker backward-merge
    # fix (4f) -- updated from the original "stands alone" expectation.
    # A non-compaction marker (background/cron/delegation) still stands
    # alone as its own boundary; see 4h.
    spans = [
        _span(1, "user", "start a long task"),
        _span(2, "assistant", "working on it"),
        _span(3, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] summary of prior work..."),
        _span(4, "assistant", "continuing"),
    ]
    result = find_segment_boundaries(spans)
    assert result == [SpanRange(1, 4, closed=False)]


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


def test_status_lookahead_stops_at_and_includes_the_first_real_user_reply():
    # 2026-11-24: 97.8% of 'unknown' items in the first G7 eval had ZERO
    # user rows in their own segment at all -- any confirmation lives in
    # the NEXT segment, which classify_verdict never gets to see. This
    # widens the search past the segment's own end, but only up to and
    # including the next REAL user message -- that first reply is what
    # answers the work, per the review comment.
    rows_after = [
        _span(10, "assistant", "still finishing up"),
        _span(11, "user", "looks good, thanks"),
        _span(12, "assistant", "glad to hear it"),
    ]
    result = find_status_lookahead_spans(rows_after, cap=20)
    assert [s["id"] for s in result] == [11]


def test_status_lookahead_skips_synthetic_markers_on_the_way_to_a_real_reply():
    rows_after = [
        _span(10, "user", "[CONTEXT COMPACTION — REFERENCE ONLY] a marker, not a real reply"),
        _span(11, "assistant", "continuing"),
        _span(12, "user", "yep, confirmed"),
    ]
    result = find_status_lookahead_spans(rows_after, cap=20)
    assert [s["id"] for s in result] == [12]


def test_status_lookahead_returns_nothing_if_no_real_user_reply_within_the_cap():
    rows_after = [
        _span(10, "assistant", "a"),
        _span(11, "assistant", "b"),
        _span(12, "assistant", "c"),
    ]
    result = find_status_lookahead_spans(rows_after, cap=20)
    assert result == []


def test_status_lookahead_respects_the_cap_even_if_a_real_reply_is_further_out():
    rows_after = [_span(10 + i, "assistant", "filler") for i in range(5)]
    rows_after.append(_span(20, "user", "confirmed, all good"))
    result = find_status_lookahead_spans(rows_after, cap=3)
    assert result == []


def test_status_lookahead_empty_input_returns_nothing():
    assert find_status_lookahead_spans([], cap=20) == []
