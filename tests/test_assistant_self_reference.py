"""Spec 4 T4.2 target tests: assistant self-reference must never leak as a
user claim, and a mixed reply must only surface the you-part.

These are written ahead of T4.2 (grounded_claims gaining a `speaker` arg) and
are EXPECTED TO FAIL until that task lands -- this file documents the two
gaps flagged in the Spec 4 review and is the RED half of tests-first for
that follow-up task, not for T4.1.
"""
from hermes_memory.model_extract import grounded_claims


def test_assistant_self_reference_produces_no_user_claim():
    """'I think the bug is in provider.py' is the ASSISTANT's own statement
    about itself, not about the user. It must produce zero claims."""
    raw = {"claims": [
        {"span_id": 1, "subject": "I", "verb": "works_on", "object": "provider.py",
         "polarity": "positive"},
    ]}
    claims = grounded_claims(
        raw, "I think the bug is in provider.py", [], {"works_on"}, speaker="assistant",
    )
    assert claims == []


def test_assistant_plural_self_reference_produces_no_user_claim():
    """'we should migrate' is the assistant speaking collectively about
    itself/the team, not a user fact. Must produce zero claims."""
    raw = {"claims": [
        {"span_id": 1, "subject": "we", "verb": "uses", "object": "migrate",
         "polarity": "positive"},
    ]}
    claims = grounded_claims(
        raw, "we should migrate", [], {"uses"}, speaker="assistant",
    )
    assert claims == []


def test_mixed_assistant_reply_keeps_only_the_you_part():
    """\"I'll remember that you live in Austin\" mixes an assistant
    self-reference ('I'll remember') with a you-claim about the user. Only
    the you-part should surface, remapped you->I, unconfirmed."""
    raw = {"claims": [
        {"span_id": 1, "subject": "I", "verb": "uses", "object": "remember",
         "polarity": "positive"},
        {"span_id": 1, "subject": "you", "verb": "lives_in", "object": "Austin",
         "polarity": "positive"},
    ]}
    claims = grounded_claims(
        raw, "I'll remember that you live in Austin", [], {"uses", "lives_in"},
        speaker="assistant",
    )
    assert len(claims) == 1
    assert claims[0]["subject"] == "I"
    assert claims[0]["verb"] == "lives_in"
    assert claims[0]["object"] == "Austin"
