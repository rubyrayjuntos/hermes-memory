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


# 2026-11-20 fix: classify_verdict matched bare "placeholder"/"verified"/
# etc. anywhere in a span, including long pasted documents and policy
# prompts that merely discuss the concept. Real-corpus false positives:
# a PDLC quality-gate prompt containing "Placeholder tests are REJECTED"
# (classified 160/610 sessions "failed" from sheer coincidence of
# wording), a code-review task prompt containing "is verified" (about a
# golden fixture, not the delivered work), and an image-generation
# request containing "fixed and verified" (describing a desired render,
# not confirming one). None of these are the user handing down a verdict.


def test_placeholder_inside_long_policy_prompt_is_not_a_verdict():
    """The actual production false positive: a PDLC quality-gate prompt
    using "placeholder" as a rejected-pattern name, not a user calling
    delivered work fake."""
    long_prompt = (
        "# PDLC HARD POLICY (factory-enforced)\n"
        "# 1. Write deliverables immediately — empty completion is failure.\n"
        "A deterministic scan REJECTS the handoff on any of these:\n\n"
        "1. **Placeholder tests** — `expect(true).toBe(true)`, `assert True`, "
        "or any trivially-true assertion that proves nothing.\n"
        + ("padding to push this well past the 300-char verdict-span limit. " * 3)
    )
    status, reason = infer_status(user_verdicts=[long_prompt], work_event_status_codes=[])
    assert status == "unknown"
    assert reason is None


def test_verdict_span_over_length_limit_is_ignored():
    """A real verdict is a short reactive statement. Any span over the
    length cap is policy/document/pasted-content territory, not a user
    reacting to delivered work -- skip classification entirely rather
    than let a long document's incidental wording decide the status."""
    long_text = "that's fake, this never worked. " + ("filler " * 100)
    assert len(long_text) > 300
    status, reason = infer_status(user_verdicts=[long_text], work_event_status_codes=[])
    assert status == "unknown"
    assert reason is None


def test_short_negative_verdict_still_classifies_under_the_limit():
    """The length cap must not blanket-suppress real short verdicts."""
    status, reason = infer_status(
        user_verdicts=["that's fake, this never worked"], work_event_status_codes=[],
    )
    assert status == "failed"
    assert reason


def test_code_review_prompt_mentioning_verified_fixture_is_not_a_verdict():
    """Leftover false positive #1 (still 'done' after the length-cap
    fix alone): a code-review TASK PROMPT referencing an already-verified
    golden fixture is not the user confirming the agent's own work."""
    status, reason = infer_status(
        user_verdicts=[
            "Code-quality review of Spec 2 on /tmp/hermes-memory-spec2 (cc3d707). "
            "Read-only. G2 golden is verified at scratch/spec2-golden-v2.json. "
            "Verdict APPROVED or REQUEST_CHANGES."
        ],
        work_event_status_codes=[],
    )
    assert status == "unknown"
    assert reason is None


def test_image_request_mentioning_verified_render_is_not_a_verdict():
    """Leftover false positive #2: an image-generation REQUEST describing
    a desired end state ("fixed and verified before render") is not a
    confirmation of delivered work."""
    status, reason = infer_status(
        user_verdicts=[
            "modern glass cabin on your island terrain, golden-hour Cycles "
            "exterior, with the material-assignment bug fixed and verified "
            "before render."
        ],
        work_event_status_codes=[],
    )
    assert status == "unknown"
    assert reason is None


def test_short_standalone_confirmed_is_still_done():
    """The narrowed confirmed/verified scoping must not lose the genuine
    short reactive case it was built to catch."""
    status, reason = infer_status(
        user_verdicts=["Confirmed — that matches what I expected, thanks!"],
        work_event_status_codes=[],
    )
    assert status == "done"
    assert reason


def test_verified_late_in_an_otherwise_short_message_still_counts():
    """'verified' near the start of a short reactive message counts;
    the position scoping is about excluding BURIED mentions deep in an
    unrelated sentence, not about punishing short messages generally."""
    status, reason = infer_status(
        user_verdicts=["Yes, verified and working now."], work_event_status_codes=[],
    )
    assert status == "done"
    assert reason
