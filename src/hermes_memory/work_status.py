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
policies warns against (see _USER_VERDICT_RES below). A success
(status_code == 0) is NOT, on its own, treated as that authoritative
confirmation: a command can exit 0 while the actual work failed, so
letting any success code alone mark 'done' would rebuild, one level
down, exactly the false confirmation Spec 4 was designed to prevent.
Only a FAILURE status_code promotes status (to 'failed') without a
user verdict; a success code with nothing else leaves status
'unknown' until either a user verdict or a real per-tool contract
exists. Tier 3 therefore stays an honest, named gap rather than an
untested promotion rule.

An explicit retraction ('retracted') is a separate user OPERATION, not
a model/rule prediction -- infer_status() never returns 'retracted'.
See store_work.py's retract_work_item for that path.

When user_verdicts contains more than one unambiguous verdict (e.g. an
earlier "never mind" and a later "actually, confirmed, go ahead"), the
LATEST one wins -- callers must pass verdicts in chronological order.
This matters most for 'abandoned': it is still the system inferring
something from text, which is acceptable only because it is the
lowest-priority signal within tier 1 AND a later user turn can always
override it.

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
    cannot name an id).

    user_verdicts must be in chronological order -- the LAST matching
    verdict wins over any earlier one, so a later turn can always
    override an earlier abandon/confirm/fail call."""
    last_verdict: Optional[str] = None
    for text in user_verdicts:
        verdict = classify_verdict(text)
        if verdict is not None:
            last_verdict = verdict
    if last_verdict is not None:
        return last_verdict, f"explicit user verdict: {last_verdict}"

    codes = [c for c in work_event_status_codes if c is not None]
    if codes:
        if any(c != 0 for c in codes):
            return "failed", "tool/eval status_code reported a non-zero (failure) result"
        # A success code alone is NOT an authoritative confirmation (see
        # module docstring) -- it can exit 0 while the real work failed.
        return "unknown", None

    return "unknown", None
