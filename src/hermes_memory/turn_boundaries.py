"""Spec 7 T7.1: boundary detection between distinct tasks within one session.

Pure (no DB, no I/O) -- same convention as ``work_status.classify_verdict``
and ``extraction_worker._is_synthetic_user_row``. See
docs/hermes-memory-dev-spec.md section 7.1 for the full design rationale,
including why the originally-drafted continuation-cue exemption list was
cut after a real-corpus review found 0/5 cues had a matching example.

Only signals 1+2 from 7.1 are implemented here. Signal 3 (touched-file/
repo-set divergence) is explicitly deferred -- it is blocked on
``work_event.refs`` existing (T5.5b, "not yet built" per Spec 5.3) and is
additive-only (it can merge a false split, never create one), so its
absence changes no test in this module; adding it later is a new,
backward-compatible optional parameter, not a signature break.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .extraction_worker import _is_synthetic_user_row


@dataclass(frozen=True)
class SpanRange:
    first_span_id: int
    last_span_id: int
    closed: bool


def find_segment_boundaries(spans: Sequence[Mapping[str, Any]]) -> list[SpanRange]:
    """One ``SpanRange`` per detected task segment, in span-id order.

    ``spans`` is the session's durable ``conversations`` rows (``id``,
    ``role``, ``content``), already sorted ascending by ``id`` -- callers
    own that ordering; this function does not re-sort (a caller passing an
    out-of-order list gets an out-of-order, likely-wrong result, same as
    every other span-id-indexed function in this codebase).

    Boundary rules (7.1):
      1. Any synthetic user-role row (``_is_synthetic_user_row``) is a hard
         boundary: it always closes the current segment and opens a new one
         starting at the marker row itself, regardless of whether work has
         happened since the segment opened.
      2. Any other (non-synthetic) user-role row is a boundary once at least
         one assistant-role row has appeared since the current segment
         opened -- i.e. the session did some work, then got a new ask. No
         exemptions (reviewed and cut, 7.1).
      3. Not implemented here -- see the module docstring.

    The last segment's ``closed`` is always ``False`` -- nothing closed it
    from this function's point of view. T7.2 (the live enqueue path) is
    the only caller today and it is only ever invoked at session end, so
    it treats every returned segment, closed or not, as ready to enqueue.
    """
    if not spans:
        return []

    segments: list[SpanRange] = []
    seg_start = int(spans[0]["id"])
    seen_assistant = False
    prev_id = seg_start

    for span in spans:
        span_id = int(span["id"])
        role = span.get("role")

        if role == "user":
            content = span.get("content") or ""
            is_marker = _is_synthetic_user_row(content)
            if is_marker:
                if span_id != seg_start:
                    segments.append(SpanRange(seg_start, prev_id, closed=True))
                seg_start = span_id
                seen_assistant = False
            elif seen_assistant:
                if span_id != seg_start:
                    segments.append(SpanRange(seg_start, prev_id, closed=True))
                seg_start = span_id
                seen_assistant = False
        elif role == "assistant":
            seen_assistant = True

        prev_id = span_id

    segments.append(SpanRange(seg_start, prev_id, closed=False))
    return _merge_marker_only_segments(segments, spans)


def _merge_marker_only_segments(
    segments: list[SpanRange], spans: Sequence[Mapping[str, Any]],
) -> list[SpanRange]:
    """2026-11-24 fix: a marker should never stand alone as its own item.

    Signal 1 makes every marker a hard boundary unconditionally, which is
    right for closing off whatever came *before* it -- but it also means a
    marker immediately followed by another hard boundary (another marker,
    or a real ask after an assistant turn) produces a degenerate segment
    containing nothing but the marker. The first G7 eval run measured this
    directly: 450/3327 items (13.5%) were marker-only, concentrated in
    sessions with heavy compaction activity (one session alone produced
    331 items). A marker-only segment (first_span_id == last_span_id, and
    that single span is itself a synthetic marker -- by construction of
    signal 1, a marker-only segment can never contain more than one span)
    merges into its neighbor: forward into the next segment if one exists,
    otherwise backward into the previous one. A lone marker with no
    neighbor at all (the entire session is just that marker) has nothing
    to merge into and stays as itself -- the existing ask-selection
    fallback-to-marker behavior already covers that case correctly.
    """
    by_id = {int(s["id"]): s for s in spans}
    # Don't assume span ids are a contiguous integer range within one
    # session -- conversations.id is a globally serial key, so another
    # session's concurrent insert can leave a gap. Filter the real spans
    # list by id membership in [first, last] instead of range()'ing the
    # integers themselves.
    ordered_ids = [int(s["id"]) for s in spans]

    def is_marker_only(seg: SpanRange) -> bool:
        ids_in_range = [sid for sid in ordered_ids if seg.first_span_id <= sid <= seg.last_span_id]
        if not ids_in_range:
            return False
        for sid in ids_in_range:
            span = by_id[sid]
            if span.get("role") != "user":
                return False
            if not _is_synthetic_user_row(span.get("content") or ""):
                return False
        return True

    merged = list(segments)
    changed = True
    while changed and len(merged) > 1:
        changed = False
        for i, seg in enumerate(merged):
            if not is_marker_only(seg):
                continue
            if i + 1 < len(merged):
                nxt = merged[i + 1]
                combined = SpanRange(seg.first_span_id, nxt.last_span_id, closed=nxt.closed)
                merged = merged[:i] + [combined] + merged[i + 2 :]
            else:
                prev = merged[i - 1]
                combined = SpanRange(prev.first_span_id, seg.last_span_id, closed=seg.closed)
                merged = merged[: i - 1] + [combined]
            changed = True
            break
    return merged
