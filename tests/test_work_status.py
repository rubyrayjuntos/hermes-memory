"""Spec 5 T5.4: status-source priority.

Priority order per the spec: (1) an explicit, unambiguous user verdict
span, (2) eval/test output in work_event, (3) a tool status only when
the tool contract makes it authoritative -- folded into (2) in this
implementation (see work_status.py's module docstring for why), else
'unknown'. Phrases like "works, thanks" must NOT count as a verdict
without a tested cue/label policy -- that is exactly what these tests
guard against.
"""
from __future__ import annotations

from hermes_memory.work_status import infer_status


def test_explicit_negative_verdict_is_failed():
    status, reason = infer_status(
        user_verdicts=["these numbers are fake / placeholder"], work_event_status_codes=[],
    )
    assert status == "failed"
    assert reason


def test_explicit_abandon_cue_is_abandoned():
    status, reason = infer_status(
        user_verdicts=["never mind, let's drop this"], work_event_status_codes=[],
    )
    assert status == "abandoned"
    assert reason


def test_explicit_positive_confirmation_is_done():
    status, reason = infer_status(
        user_verdicts=["confirmed, that's correct"], work_event_status_codes=[],
    )
    assert status == "done"


def test_loose_acknowledgement_is_not_a_verdict():
    """The spec's own named caution: "works, thanks" must not be treated
    as a verdict without a tested cue/label policy."""
    status, reason = infer_status(
        user_verdicts=["works, thanks"], work_event_status_codes=[],
    )
    assert status == "unknown"


def test_bare_thanks_is_not_a_verdict():
    status, _ = infer_status(user_verdicts=["Thanks!"], work_event_status_codes=[])
    assert status == "unknown"


def test_bare_great_is_not_a_verdict():
    status, _ = infer_status(user_verdicts=["Great, looks good overall."], work_event_status_codes=[])
    assert status == "unknown"


def test_success_code_alone_does_not_confirm_done():
    """Tightened rule: folding tiers 2+3 together must not let any tool's
    bare exit-0 mark an item done on its own -- a command can exit 0
    while the actual work failed, which would rebuild, one level down,
    exactly the false confirmation Spec 4 was built to prevent. Only a
    user verdict or a real per-tool contract (not built here) can
    confirm 'done'; a success code with nothing else stays 'unknown'."""
    status, reason = infer_status(user_verdicts=[], work_event_status_codes=[0])
    assert status == "unknown"
    assert reason is None


def test_eval_output_failure_is_failed_when_no_user_verdict():
    status, reason = infer_status(user_verdicts=[], work_event_status_codes=[1])
    assert status == "failed"
    assert reason


def test_mixed_eval_outputs_any_failure_wins():
    status, _ = infer_status(user_verdicts=[], work_event_status_codes=[0, 1, 0])
    assert status == "failed"


def test_all_success_codes_stay_unknown():
    status, _ = infer_status(user_verdicts=[], work_event_status_codes=[0, 0, 0])
    assert status == "unknown"


def test_no_evidence_at_all_is_unknown():
    status, reason = infer_status(user_verdicts=[], work_event_status_codes=[])
    assert status == "unknown"
    assert reason is None


def test_user_verdict_outranks_eval_output():
    """Tier 1 (explicit user verdict) wins even when tier 2 (eval output)
    would say something different."""
    status, _ = infer_status(
        user_verdicts=["these numbers are fake / placeholder"],
        work_event_status_codes=[0],
    )
    assert status == "failed"


def test_non_verdict_user_text_does_not_block_eval_output():
    """A user turn with ordinary non-verdict content must not suppress
    tier 2 just because a user turn exists in the range. Uses a failure
    code (the only code that promotes status on its own) to isolate
    this from the separate success-alone-stays-unknown rule above."""
    status, _ = infer_status(
        user_verdicts=["Can you also check the other file?"],
        work_event_status_codes=[1],
    )
    assert status == "failed"


def test_later_user_verdict_overrides_an_earlier_one():
    """abandoned is still the system inferring something from text --
    acceptable only because it is the lowest-priority signal AND a
    later user turn can override it. A later 'confirmed' must win over
    an earlier 'never mind', not the reverse."""
    status, reason = infer_status(
        user_verdicts=["never mind, let's drop this", "actually, confirmed, go ahead"],
        work_event_status_codes=[],
    )
    assert status == "done"
    assert reason


def test_later_abandon_overrides_an_earlier_confirmation():
    """The override is symmetric by recency, not a special case for one
    direction: a later 'never mind' must also win over an earlier
    'confirmed'."""
    status, _ = infer_status(
        user_verdicts=["confirmed, go ahead", "actually, never mind, forget it"],
        work_event_status_codes=[],
    )
    assert status == "abandoned"


def test_never_infers_failed_from_a_missing_artifact():
    """Named explicitly in the spec: never infer 'failed' from a missing
    git object. infer_status has no artifact-awareness at all -- it
    cannot see one, by construction, which is the safe default this
    test pins down."""
    status, _ = infer_status(user_verdicts=[], work_event_status_codes=[])
    assert status != "failed"
