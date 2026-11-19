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


def test_eval_output_success_is_done_when_no_user_verdict():
    status, reason = infer_status(user_verdicts=[], work_event_status_codes=[0])
    assert status == "done"
    assert reason


def test_eval_output_failure_is_failed_when_no_user_verdict():
    status, reason = infer_status(user_verdicts=[], work_event_status_codes=[1])
    assert status == "failed"
    assert reason


def test_mixed_eval_outputs_any_failure_wins():
    status, _ = infer_status(user_verdicts=[], work_event_status_codes=[0, 1, 0])
    assert status == "failed"


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
    tier 2 just because a user turn exists in the range."""
    status, _ = infer_status(
        user_verdicts=["Can you also check the other file?"],
        work_event_status_codes=[0],
    )
    assert status == "done"


def test_never_infers_failed_from_a_missing_artifact():
    """Named explicitly in the spec: never infer 'failed' from a missing
    git object. infer_status has no artifact-awareness at all -- it
    cannot see one, by construction, which is the safe default this
    test pins down."""
    status, _ = infer_status(user_verdicts=[], work_event_status_codes=[])
    assert status != "failed"
