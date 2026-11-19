"""Spec 5 T5.4: status-source priority for work_item.status.

Priority order (from the spec, verbatim):
  1. An explicit, unambiguous user verdict span.
  2. Eval/test output in work_event.
  3. A tool status only when the tool contract makes it authoritative.
  4. With no evidence, status is 'unknown'.

Tier 3 is folded into tier 2 here: this codebase has no existing concept
of a per-tool "authoritative contract" distinguishing which tools are
trustworthy status sources, and inventing one without testing it is
exactly what the spec's own caution about unvalidated cue/label
policies warns against (see _USER_VERDICT_RES below). work_event's
status_code is the only structural signal available in this slice, so
every status_code already plays tier 2+3's combined role; a real
authoritative-tool-contract system is a named follow-up, not built
here.

An explicit retraction ('retracted') is a separate user OPERATION, not
a model/rule prediction -- infer_status() never returns 'retracted'.
See store_work.py's retract_work_item for that path.

Pure functions only -- no I/O, same convention as reception.py.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

# Tight, explicit verdict cues only. Deliberately NOT a loose sentiment
# check: "works, thanks" / "Thanks!" / "Great, looks good" must never
# match ANY of these -- the spec names this exact failure mode. Every
# pattern here was tested against that hazard before being added.
_NEGATIVE_VERDICT_RES = [
    re.compile(r"(?i)\b(fake|placeholder|fabricated|made up|made-up)\b"),
    re.compile(r"(?i)\bthat'?s (wrong|incorrect|not right)\b"),
    re.compile(r"(?i)\bdid(n'?t| not) (really )?(work|pass)\b"),
    re.compile(r"(?i)\b(is|was) broken\b"),
    re.compile(r"(?i)\bnot real\b"),
]

_ABANDON_VERDICT_RES = [
    re.compile(r"(?i)\bnever mind\b"),
    re.compile(r"(?i)\bforget it\b"),
    re.compile(r"(?i)\bdon'?t bother\b"),
    re.compile(r"(?i)\bdrop (this|it)\b"),
    re.compile(r"(?i)\babandon (this|it)\b"),
]

# Narrow and explicit on purpose: generic positive sentiment ("thanks",
# "great", "works") is excluded by construction -- none of these words
# appear in any pattern below.
_POSITIVE_VERDICT_RES = [
    re.compile(r"(?i)\bconfirmed\b"),
    re.compile(r"(?i)\bverified\b"),
    re.compile(r"(?i)\bthat'?s (correct|accurate|right)\b"),
    re.compile(r"(?i)\byes,?\s*that'?s right\b"),
]


def classify_verdict(text: str) -> Optional[str]:
    """'failed' | 'abandoned' | 'done' | None (no unambiguous verdict).
    Exposed (not module-private) so a caller needing to attribute a
    verdict to a specific span/event id can classify one item at a time
    with the exact same logic infer_status uses in aggregate."""
    if any(p.search(text) for p in _NEGATIVE_VERDICT_RES):
        return "failed"
    if any(p.search(text) for p in _ABANDON_VERDICT_RES):
        return "abandoned"
    if any(p.search(text) for p in _POSITIVE_VERDICT_RES):
        return "done"
    return None


def infer_status(
    *, user_verdicts: Sequence[str], work_event_status_codes: Sequence[Optional[int]],
) -> tuple[str, Optional[str]]:
    """(status, reason). status is always one of done|failed|abandoned|
    unknown -- never 'retracted' (see module docstring). reason is None
    only when status is 'unknown' with no evidence at all; callers that
    need reason_span_id/reason_event_id attach those separately (see
    classify_verdict), keyed off which user_verdicts entry or work_event
    row this reason came from (infer_status itself is text-only, so it
    cannot name an id)."""
    for text in user_verdicts:
        verdict = classify_verdict(text)
        if verdict is not None:
            return verdict, f"explicit user verdict: {verdict}"

    codes = [c for c in work_event_status_codes if c is not None]
    if codes:
        if any(c != 0 for c in codes):
            return "failed", "tool/eval status_code reported a non-zero (failure) result"
        return "done", "tool/eval status_code reported success (0) with no contrary user verdict"

    return "unknown", None
